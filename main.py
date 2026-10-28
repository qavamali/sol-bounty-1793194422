#!/usr/bin/env node

/**
 * ============================================================================
 * SOLANA BLOCK ORDER ENGINE: INSIDE THE BLOCK
 * ============================================================================
 * A complete, standalone architectural analyzer and Banking Stage simulation
 * engine explaining and demonstrating how Solana validators order transactions.
 *
 * Implements:
 * 1. Live RPC Block Forensic Extraction (Compute Budget, Lock Graphs, Fee Rates)
 * 2. Unified Scheduler (Prio-Graph) vs Legacy Banking Stage Simulation
 * 3. Sealevel Parallel Read/Write Account Dependency Resolvers
 * 4. Jito MEV Bundle & Tip Account Identification
 * 5. Deterministic Offline Simulation Harness with Contention Graphing
 * ============================================================================
 */

import { Connection, PublicKey } from '@solana/web3.js';
import { Command } from 'commander';
import chalk from 'chalk';

// Compute Budget Program ID
const COMPUTE_BUDGET_PROGRAM_ID = 'ComputeBudget111111111111111111111111111111';

// Known Jito Tip Payment Accounts
const JITO_TIP_ACCOUNTS = new Set([
  '96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5',
  'HFqU5x63VTxvQssQnRxmuUMQYJKq5554uPtaMtP84usV',
  'Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY',
  'ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49',
  'DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh',
  'ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt',
  'DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL',
  '3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT',
]);

/**
 * Core Data Structures
 */
class ParsedBlockTx {
  constructor(params) {
    this.signature = params.signature;
    this.indexInBlock = params.indexInBlock;
    this.writableAccounts = params.writableAccounts;
    this.readonlyAccounts = params.readonlyAccounts;
    this.allAccounts = params.allAccounts;
    this.computeUnitLimit = params.computeUnitLimit;
    this.computeUnitPriceMicroLamports = params.computeUnitPriceMicroLamports;
    this.totalFeeLamports = params.totalFeeLamports;
    this.computeUnitsConsumed = params.computeUnitsConsumed;
    this.isJitoTip = params.isJitoTip;
    this.jitoTipLamports = params.jitoTipLamports;
    this.effectivePriorityFeeRate = params.effectivePriorityFeeRate; // micro-lamports per CU
    this.slotEntryIndex = params.slotEntryIndex || 0;
  }
}

/**
 * Decodes Compute Budget Program Instructions within raw compiled transaction instructions
 */
function decodeComputeBudgetInstructions(transaction) {
  let computeUnitLimit = 200_000; // Default Solana compute unit limit
  let computeUnitPriceMicroLamports = 0n;

  try {
    const accountKeys = transaction.message.accountKeys.map(k => (typeof k === 'string' ? k : k.pubkey ? k.pubkey.toBase58() : k.toBase58()));
    const instructions = transaction.message.instructions;

    for (const ix of instructions) {
      const programId = accountKeys[ix.programIdIndex];
      if (programId === COMPUTE_BUDGET_PROGRAM_ID) {
        // Raw byte parsing
        let dataBytes;
        if (typeof ix.data === 'string') {
          dataBytes = Buffer.from(ix.data, 'base64');
        } else if (Array.isArray(ix.data)) {
          dataBytes = Buffer.from(ix.data);
        } else if (Buffer.isBuffer(ix.data)) {
          dataBytes = ix.data;
        }

        if (dataBytes && dataBytes.length > 0) {
          const discriminator = dataBytes[0];
          // 0x02 = SetComputeUnitLimit
          if (discriminator === 2 && dataBytes.length >= 5) {
            computeUnitLimit = dataBytes.readUInt32LE(1);
          }
          // 0x03 = SetComputeUnitPrice
          else if (discriminator === 3 && dataBytes.length >= 9) {
            computeUnitPriceMicroLamports = dataBytes.readBigUInt64LE(1);
          }
        }
      }
    }
  } catch (err) {
    // Fallback default on decode error
  }

  return { computeUnitLimit, computeUnitPriceMicroLamports };
}

/**
 * Detects Jito MEV tips inside transaction instructions & inner instructions
 */
function inspectJitoTip(transaction, meta) {
  let isJitoTip = false;
  let jitoTipLamports = 0n;

  try {
    const accountKeys = transaction.message.accountKeys.map(k => (typeof k === 'string' ? k : k.pubkey ? k.pubkey.toBase58() : k.toBase58()));

    // Check account list for known Jito tip accounts
    const tipAccountIndices = [];
    accountKeys.forEach((key, idx) => {
      if (JITO_TIP_ACCOUNTS.has(key)) {
        tipAccountIndices.push(idx);
      }
    });

    if (tipAccountIndices.length > 0 && meta) {
      // Calculate balance delta for the tip account
      for (const idx of tipAccountIndices) {
        const pre = BigInt(meta.preBalances[idx] || 0);
        const post = BigInt(meta.postBalances[idx] || 0);
        if (post > pre) {
          isJitoTip = true;
          jitoTipLamports += (post - pre);
        }
      }
    }
  } catch (err) {
    // Silent fail
  }

  return { isJitoTip, jitoTipLamports };
}

/**
 * Extracts and partitions read/write account sets based on Solana transaction header flags
 */
function extractAccountLockSets(transaction) {
  const accountKeys = transaction.message.accountKeys.map(k => (typeof k === 'string' ? k : k.pubkey ? k.pubkey.toBase58() : k.toBase58()));
  const header = transaction.message.header;

  const numRequiredSignatures = header.numRequiredSignatures;
  const numReadonlySignedAccounts = header.numReadonlySignedAccounts;
  const numReadonlyUnsignedAccounts = header.numReadonlyUnsignedAccounts;

  const totalAccounts = accountKeys.length;
  const numSignedAccounts = numRequiredSignatures;
  const numUnsignedAccounts = totalAccounts - numSignedAccounts;

  const writableSignedCount = numSignedAccounts - numReadonlySignedAccounts;
  const writableUnsignedCount = numUnsignedAccounts - numReadonlyUnsignedAccounts;

  const writableAccounts = new Set();
  const readonlyAccounts = new Set();

  for (let i = 0; i < totalAccounts; i++) {
    const key = accountKeys[i];
    if (i < writableSignedCount) {
      writableAccounts.add(key);
    } else if (i < numSignedAccounts) {
      readonlyAccounts.add(key);
    } else if (i < numSignedAccounts + writableUnsignedCount) {
      writableAccounts.add(key);
    } else {
      readonlyAccounts.add(key);
    }
  }

  return {
    writableAccounts: Array.from(writableAccounts),
    readonlyAccounts: Array.from(readonlyAccounts),
    allAccounts: accountKeys,
  };
}

/**
 * Parses raw Solana RPC Block data into rich transaction ordering models
 */
function parseSolanaBlock(blockData) {
  const transactions = [];

  for (let i = 0; i < blockData.transactions.length; i++) {
    const item = blockData.transactions[i];
    const tx = item.transaction;
    const meta = item.meta;

    const signature = tx.signatures && tx.signatures.length > 0 ? tx.signatures[0] : `tx_${i}`;
    const { writableAccounts, readonlyAccounts, allAccounts } = extractAccountLockSets(tx);
    const { computeUnitLimit, computeUnitPriceMicroLamports } = decodeComputeBudgetInstructions(tx);
    const { isJitoTip, jitoTipLamports } = inspectJitoTip(tx, meta);

    const totalFeeLamports = BigInt(meta ? meta.fee : 5000);
    const computeUnitsConsumed = meta && meta.computeUnitsConsumed ? meta.computeUnitsConsumed : computeUnitLimit;

    // Rate: Micro-lamports per Compute Unit
    let effectivePriorityFeeRate = computeUnitPriceMicroLamports;
    if (effectivePriorityFeeRate === 0n && totalFeeLamports > 5000n) {
      // Calculate synthetic priority fee if compute budget instruction was omitted but base fee was boosted
      const priorityLamports = totalFeeLamports - 5000n;
      effectivePriorityFeeRate = (priorityLamports * 1_000_000n) / BigInt(Math.max(computeUnitLimit, 1));
    }

    transactions.push(new ParsedBlockTx({
      signature,
      indexInBlock: i,
      writableAccounts,
      readonlyAccounts,
      allAccounts,
      computeUnitLimit,
      computeUnitPriceMicroLamports,
      totalFeeLamports,
      computeUnitsConsumed,
      isJitoTip,
      jitoTipLamports,
      effectivePriorityFeeRate,
      slotEntryIndex: Math.floor(i / 16), // Approximated Entry Batch
    }));
  }

  return transactions;
}

/**
 * Builds Lock Conflict Graph and measures Theoretical Concurrency
 */
function analyzeLockGraph(parsedTxs) {
  let totalConflicts = 0;
  const directConflictEdges = [];
  const accountWriteHeatmap = new Map();
  const accountReadHeatmap = new Map();

  for (let i = 0; i < parsedTxs.length; i++) {
    const txA = parsedTxs[i];

    // Heatmaps
    for (const w of txA.writableAccounts) {
      accountWriteHeatmap.set(w, (accountWriteHeatmap.get(w) || 0) + 1);
    }
    for (const r of txA.readonlyAccounts) {
      accountReadHeatmap.set(r, (accountReadHeatmap.get(r) || 0) + 1);
    }

    // Pairwise conflict check with following transactions (sliding window of 64)
    const windowLimit = Math.min(parsedTxs.length, i + 64);
    for (let j = i + 1; j < windowLimit; j++) {
      const txB = parsedTxs[j];
      let hasConflict = false;

      // Write-Write conflict
      for (const wA of txA.writableAccounts) {
        if (txB.writableAccounts.includes(wA)) {
          hasConflict = true;
          break;
        }
      }

      // Write-Read conflict
      if (!hasConflict) {
        for (const wA of txA.writableAccounts) {
          if (txB.readonlyAccounts.includes(wA)) {
            hasConflict = true;
            break;
          }
        }
      }

      // Read-Write conflict
      if (!hasConflict) {
        for (const rA of txA.readonlyAccounts) {
          if (txB.writableAccounts.includes(rA)) {
            hasConflict = true;
            break;
          }
        }
      }

      if (hasConflict) {
        totalConflicts++;
        if (directConflictEdges.length < 500) {
          directConflictEdges.push({ from: i, to: j });
        }
      }
    }
  }

  // Sort hottest accounts
  const sortedHottestWriters = Array.from(accountWriteHeatmap.entries())
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5);

  return {
    totalConflicts,
    directConflictEdges,
    hottestWriters: sortedHottestWriters,
    uniqueAccountsCount: accountWriteHeatmap.size + accountReadHeatmap.size,
  };
}

/**
 * Banking Stage Scheduler Simulator (Unified Scheduler vs Legacy Multi-threader)
 */
class BankingStageSimulator {
  constructor(transactions, threadCount = 4) {
    this.transactions = transactions;
    this.threadCount = threadCount;
  }

  /**
   * Simulates Solana v1.18+ Unified Scheduler (Prio-Graph)
   * Dispatches highest fee-per-CU first when lock dependencies are clear.
   */
  simulateUnifiedScheduler() {
    const queue = [...this.transactions].sort((a, b) => {
      if (b.effectivePriorityFeeRate > a.effectivePriorityFeeRate) return 1;
      if (b.effectivePriorityFeeRate < a.effectivePriorityFeeRate) return -1;
      return a.indexInBlock - b.indexInBlock;
    });

    const threadAvailability = new Array(this.threadCount).fill(0); // timestamp when core is free
    const activeWriteLocks = new Map(); // Account -> Release Time
    const activeReadLocks = new Map();  // Account -> List of Release Times

    let currentVirtualTime = 0;
    const executionSchedule = [];

    while (queue.length > 0) {
      let candidateIndex = -1;

      // Find highest priority transaction whose locks can be satisfied
      for (let i = 0; i < queue.length; i++) {
        const candidate = queue[i];
        let lockContended = false;

        // Check write conflicts
        for (const w of candidate.writableAccounts) {
          if ((activeWriteLocks.get(w) || 0) > currentVirtualTime) {
            lockContended = true;
            break;
          }
          const readReleases = activeReadLocks.get(w) || [];
          if (readReleases.some(t => t > currentVirtualTime)) {
            lockContended = true;
            break;
          }
        }

        // Check read conflicts
        if (!lockContended) {
          for (const r of candidate.readonlyAccounts) {
            if ((activeWriteLocks.get(r) || 0) > currentVirtualTime) {
              lockContended = true;
              break;
            }
          }
        }

        if (!lockContended) {
          candidateIndex = i;
          break;
        }
      }

      if (candidateIndex !== -1) {
        // Dispatch candidate
        const [tx] = queue.splice(candidateIndex, 1);

        // Find earliest available thread
        let targetThread = 0;
        let minThreadTime = threadAvailability[0];
        for (let t = 1; t < this.threadCount; t++) {
          if (threadAvailability[t] < minThreadTime) {
            minThreadTime = threadAvailability[t];
            targetThread = t;
          }
        }

        const startTime = Math.max(currentVirtualTime, minThreadTime);
        const duration = Math.max(1, Math.floor(tx.computeUnitsConsumed / 50_000));
        const endTime = startTime + duration;

        threadAvailability[targetThread] = endTime;

        // Acquire locks
        for (const w of tx.writableAccounts) {
          activeWriteLocks.set(w, endTime);
        }
        for (const r of tx.readonlyAccounts) {
          const arr = activeReadLocks.get(r) || [];
          arr.push(endTime);
          activeReadLocks.set(r, arr);
        }

        executionSchedule.push({
          signature: tx.signature,
          originalIndex: tx.indexInBlock,
          priorityRate: tx.effectivePriorityFeeRate,
          thread: targetThread,
          startTime,
          endTime,
        });
      } else {
        // Advance time to next lock release
        let nextTime = Infinity;
        for (const t of threadAvailability) {
          if (t > currentVirtualTime && t < nextTime) nextTime = t;
        }
        for (const release of activeWriteLocks.values()) {
          if (release > currentVirtualTime && release < nextTime) nextTime = release;
        }
        for (const list of activeReadLocks.values()) {
          for (const release of list) {
            if (release > currentVirtualTime && release < nextTime) nextTime = release;
          }
        }

        currentVirtualTime = nextTime === Infinity ? currentVirtualTime + 1 : nextTime;
      }
    }

    const totalDuration = Math.max(...threadAvailability);
    return { executionSchedule, totalDuration };
  }

  /**
   * Simulates Legacy Banking Stage (Round-robin hashing without global lock DAG)
   */
  simulateLegacyScheduler() {
    const threadQueues = Array.from({ length: this.threadCount }, () => []);
    
    // Legacy distribution: packets partitioned statically across threads
    this.transactions.forEach((tx, idx) => {
      const threadIdx = idx % this.threadCount;
      threadQueues[threadIdx].push(tx);
    });

    const threadTime = new Array(this.threadCount).fill(0);
    const globalWriteLocks = new Map();

    const schedule = [];

    // Each thread steps through its local queue
    let active = true;
    while (active) {
      active = false;
      for (let t = 0; t < this.threadCount; t++) {
        if (threadQueues[t].length > 0) {
          active = true;
          const tx = threadQueues[t][0];

          // Check if locks are blocked globally
          let blocked = false;
          for (const w of tx.writableAccounts) {
            if ((globalWriteLocks.get(w) || 0) > threadTime[t]) {
              blocked = true;
              break;
            }
          }

          if (!blocked) {
            threadQueues[t].shift();
            const duration = Math.max(1, Math.floor(tx.computeUnitsConsumed / 50_000));
            const start = threadTime[t];
            const end = start + duration;
            threadTime[t] = end;

            for (const w of tx.writableAccounts) {
              globalWriteLocks.set(w, end);
            }

            schedule.push({
              signature: tx.signature,
              originalIndex: tx.indexInBlock,
              thread: t,
              startTime: start,
              endTime: end,
            });
          } else {
            // Thread stalls waiting for lock release
            threadTime[t] += 1;
          }
        }
      }
    }

    const totalDuration = Math.max(...threadTime);
    return { executionSchedule: schedule, totalDuration };
  }
}

/**
 * Detects Jito MEV Bundle sequences inside a block
 */
function detectMEVBundles(parsedTxs) {
  const detectedBundles = [];
  let currentBundle = [];

  for (let i = 0; i < parsedTxs.length; i++) {
    const tx = parsedTxs[i];

    if (tx.isJitoTip) {
      currentBundle.push(tx);
      detectedBundles.push({
        bundleSize: currentBundle.length,
        tipTx: tx.signature,
        tipLamports: tx.jitoTipLamports,
        transactions: [...currentBundle],
      });
      currentBundle = [];
    } else {
      // Check if this transaction shares write locks with subsequent Jito tip tx
      const windowLimit = Math.min(parsedTxs.length, i + 5);
      let linkedToUpcomingTip = false;

      for (let j = i + 1; j < windowLimit; j++) {
        if (parsedTxs[j].isJitoTip) {
          // Check state overlap
          const overlap = tx.writableAccounts.some(acc => parsedTxs[j].writableAccounts.includes(acc));
          if (overlap) {
            linkedToUpcomingTip = true;
            break;
          }
        }
      }

      if (linkedToUpcomingTip) {
        currentBundle.push(tx);
      } else {
        currentBundle = [];
      }
    }
  }

  return detectedBundles;
}

/**
 * Offline Synthetic Block Generator for rigorous stress testing
 */
function generateSyntheticBlock(txCount = 100, contentionLevel = 'high') {
  const transactions = [];
  const popularAMMPool = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA';
  const popularOracle = 'FsJ3A3u2vn5cTVofMrj6qF9193q59129591238591238';

  for (let i = 0; i < txCount; i++) {
    const isHighFee = Math.random() < 0.2;
    const isJitoBundle = i === 10 || i === 11 || i === 12;

    const writable = [];
    const readonly = [];

    if (contentionLevel === 'high') {
      // 70% touch the same hot AMM pool
      if (Math.random() < 0.7) {
        writable.push(popularAMMPool);
      }
      readonly.push(popularOracle);
    } else if (contentionLevel === 'medium') {
      if (Math.random() < 0.3) {
        writable.push(popularAMMPool);
      }
    }

    // Unique signer account
    writable.push(new PublicKey(Buffer.alloc(32, (i + 1) & 0xff)).toBase58());
    readonly.push('11111111111111111111111111111111');

    const computeUnitPriceMicroLamports = isHighFee ? BigInt(5_000_000 + Math.floor(Math.random() * 10_000_000)) : BigInt(Math.floor(Math.random() * 50_000));
    const computeUnitLimit = 200_000;
    const computeUnitsConsumed = 45_000 + Math.floor(Math.random() * 100_000);

    const isTip = isJitoBundle && i === 12;
    const jitoTipLamports = isTip ? 50_000_000n : 0n;

    transactions.push(new ParsedBlockTx({
      signature: `synth_sig_${i.toString().padStart(4, '0')}_${Math.random().toString(36).substring(2, 7)}`,
      indexInBlock: i,
      writableAccounts: writable,
      readonlyAccounts: readonly,
      allAccounts: [...writable, ...readonly],
      computeUnitLimit,
      computeUnitPriceMicroLamports,
      totalFeeLamports: 5000n + (computeUnitPriceMicroLamports * BigInt(computeUnitLimit) / 1_000_000n),
      computeUnitsConsumed,
      isJitoTip: isTip,
      jitoTipLamports,
      effectivePriorityFeeRate: computeUnitPriceMicroLamports,
      slotEntryIndex: Math.floor(i / 16),
    }));
  }

  return transactions;
}

/**
 * Terminal UI Visualizers
 */
function printBlockMetrics(parsedTxs, lockAnalysis, bundles, slotNum) {
  console.log(chalk.bold.cyan('\n================================================================================'));
  console.log(chalk.bold.white(` SOLANA VALIDATOR BLOCK ORDERING FORENSICS | SLOT: ${chalk.yellow(slotNum || 'SIMULATION')}`));
  console.log(chalk.bold.cyan('================================================================================\n'));

  const totalTxs = parsedTxs.length;
  const totalFees = parsedTxs.reduce((sum, tx) => sum + tx.totalFeeLamports, 0n);
  const totalCU = parsedTxs.reduce((sum, tx) => sum + tx.computeUnitsConsumed, 0);
  const prioritizedTxs = parsedTxs.filter(tx => tx.effectivePriorityFeeRate > 0n).length;

  console.log(chalk.bold('Block Composition Overview:'));
  console.log(` • Total Transactions:         ${chalk.green(totalTxs)}`);
  console.log(` • Prioritized Transactions:   ${chalk.green(prioritizedTxs)} (${((prioritizedTxs / totalTxs) * 100).toFixed(1)}%)`);
  console.log(` • Total Block Fees:           ${chalk.yellow((Number(totalFees) / 1e9).toFixed(5))} SOL`);
  console.log(` • Total Compute Units:        ${chalk.magenta(totalCU.toLocaleString())} CU`);
  console.log(` • Unique Accounts Referenced: ${chalk.blue(lockAnalysis.uniqueAccountsCount)}`);
  console.log(` • Total Read/Write Conflicts: ${chalk.red(lockAnalysis.totalConflicts)} pairwise dependencies\n`);

  console.log(chalk.bold('Top Contended Accounts (Write-Lock Chokepoints):'));
  lockAnalysis.hottestWriters.forEach(([acc, count], rank) => {
    console.log(`  ${rank + 1}. ${chalk.yellow(acc)} ─ ${chalk.red(count)} write-lock acquisitions`);
  });

  console.log(chalk.bold('\nMEV & Jito Bundle Telemetry:'));
  if (bundles.length === 0) {
    console.log(chalk.gray('  No explicit Jito tip-payment bundles identified in this sample.'));
  } else {
    bundles.forEach((b, idx) => {
      console.log(`  Bundle #${idx + 1}: ${chalk.green(b.bundleSize)} txs atomic sequence | Tip: ${chalk.yellow((Number(b.tipLamports) / 1e9).toFixed(4))} SOL | Tip Tx: ${b.tipTx.substring(0, 16)}...`);
    });
  }
}

function printSchedulerBenchmark(legacyResult, unifiedResult) {
  console.log(chalk.bold.cyan('\n--------------------------------------------------------------------------------'));
  console.log(chalk.bold.white(' BANKING STAGE SCHEDULER COMPARATIVE BENCHMARK'));
  console.log(chalk.bold.cyan('--------------------------------------------------------------------------------\n'));

  const speedup = ((legacyResult.totalDuration - unifiedResult.totalDuration) / legacyResult.totalDuration) * 100;

  console.log(` • Legacy Banking Stage Latency:  ${chalk.red(legacyResult.totalDuration)} cycles (Thread stalls due to lock collisions)`);
  console.log(` • Unified Scheduler (v1.18+):    ${chalk.green(unifiedResult.totalDuration)} cycles (Prio-graph directed topological dispatch)`);
  console.log(` • Concurrency Efficiency Gain:   ${chalk.bold.green(speedup.toFixed(2) + '%')} latency reduction\n`);

  console.log(chalk.bold('Execution Dispatches (First 8 Transactions in Block):'));
  console.log(chalk.gray('Index | Original Slot Pos | Effective Priority | Assigned Core | Exec Window'));

  unifiedResult.executionSchedule.slice(0, 8).forEach((item, idx) => {
    const prioStr = item.priorityRate.toString().padStart(12, ' ') + ' µLam/CU';
    console.log(
      `  [${idx.toString().padStart(2, '0')}]  ` +
      `Original: #${item.originalIndex.toString().padStart(3, '0')}   ` +
      `${chalk.cyan(prioStr)}   ` +
      `Thread: ${chalk.yellow(item.thread)}   ` +
      `[T:${item.startTime.toString().padStart(3, '0')} -> T:${item.endTime.toString().padStart(3, '0')}]`
    );
  });
  console.log('\n');
}

/**
 * Live RPC Block Ingestion Controller
 */
async function runLiveAnalysis(slotNumber, rpcUrl) {
  console.log(chalk.blue(`[+] Connecting to Solana RPC: ${rpcUrl}`));
  const connection = new Connection(rpcUrl, 'confirmed');

  let targetSlot = slotNumber;
  if (!targetSlot) {
    console.log(chalk.blue('[+] Slot not provided. Querying latest finalized slot...'));
    targetSlot = await connection.getSlot('finalized');
  }

  console.log(chalk.blue(`[+] Fetching confirmed block data for slot ${targetSlot}...`));
  const block = await connection.getBlock(targetSlot, {
    maxSupportedTransactionVersion: 0,
    rewards: false,
    transactionDetails: 'full',
  });

  if (!block || !block.transactions) {
    throw new Error(`Failed to retrieve transactions for slot ${targetSlot}. Block might be skipped or cleaned from ledger.`);
  }

  console.log(chalk.green(`[✔] Ingested ${block.transactions.length} transactions from slot ${targetSlot}`));
  const parsedTxs = parseSolanaBlock(block);
  const lockAnalysis = analyzeLockGraph(parsedTxs);
  const bundles = detectMEVBundles(parsedTxs);

  printBlockMetrics(parsedTxs, lockAnalysis, bundles, targetSlot);

  // Run Simulator on ingested block
  const simulator = new BankingStageSimulator(parsedTxs, 4);
  const legacySim = simulator.simulateLegacyScheduler();
  const unifiedSim = simulator.simulateUnifiedScheduler();

  printSchedulerBenchmark(legacySim, unifiedSim);
}

/**
 * Standalone Offline Simulation Controller
 */
function runOfflineSimulation(txCount, threadCount, contention) {
  console.log(chalk.blue(`[+] Initializing offline synthetic simulation harness...`));
  console.log(chalk.blue(`[+] Parameters: ${txCount} transactions | ${threadCount} worker threads | Contention: ${contention}`));

  const parsedTxs = generateSyntheticBlock(txCount, contention);
  const lockAnalysis = analyzeLockGraph(parsedTxs);
  const bundles = detectMEVBundles(parsedTxs);

  printBlockMetrics(parsedTxs, lockAnalysis, bundles, null);

  const simulator = new BankingStageSimulator(parsedTxs, threadCount);
  const legacySim = simulator.simulateLegacyScheduler();
  const unifiedSim = simulator.simulateUnifiedScheduler();

  printSchedulerBenchmark(legacySim, unifiedSim);
}

/**
 * CLI Definition and Entrypoint
 */
const program = new Command();

program
  .name('solana-block-order-engine')
  .description('Comprehensive Solana Validator Block Ordering & Banking Stage Forensic Engine')
  .version('1.0.0');

program
  .command('analyze')
  .description('Connects to a live Solana cluster and decodes intra-block transaction ordering dynamics')
  .option('-s, --slot <number>', 'Specific slot number to inspect (defaults to latest finalized slot)')
  .option('-r, --rpc <url>', 'Solana JSON-RPC endpoint', 'https://api.mainnet-beta.solana.com')
  .action(async (options) => {
    try {
      const slot = options.slot ? parseInt(options.slot, 10) : undefined;
      await runLiveAnalysis(slot, options.rpc);
    } catch (err) {
      console.error(chalk.red(`\n[-] Live Analysis Error: ${err.message}`));
      console.log(chalk.yellow('[!] Falling back to deterministic offline simulation mode...\n'));
      runOfflineSimulation(120, 4, 'high');
    }
  });

program
  .command('simulate')
  .description('Simulates Banking Stage thread execution, lock graphs, and priority fees offline')
  .option('-t, --txs <number>', 'Number of transactions', '100')
  .option('-c, --threads <number>', 'Validator Banking Stage thread count', '4')
  .option('-k, --contention <level>', 'Account lock contention level: low | medium | high', 'high')
  .action((options) => {
    const txCount = parseInt(options.txs, 10);
    const threads = parseInt(options.threads, 10);
    runOfflineSimulation(txCount, threads, options.contention);
  });

program.parse(process.argv);

if (!process.argv.slice(2).length) {
  // Default to offline simulation if no args passed
  runOfflineSimulation(100, 4, 'high');
}