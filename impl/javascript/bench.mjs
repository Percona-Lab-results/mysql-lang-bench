// MySQL language benchmark - JavaScript implementation (Node.js + mysql2).
//
// Follows SPEC.md v2 exactly: multithreaded sysbench-oltp_read_write-style
// transactional workload. One worker_threads Worker per benchmark worker
// (real OS thread, own V8 isolate), each with its own mysql2 connection
// (no pools; autocommit ON, explicit BEGIN/COMMIT as plain .query() calls),
// its own 31-bit LCG PRNG seeded BENCH_SEED + worker_index, barrier-based
// timing coordinated over postMessage, retry on MySQL errors 1213/1205/1062
// with full value redraw, per-transaction latency in integer microseconds,
// one JSON result line on stdout.
//
// mysql2's .execute() is a server-side prepared statement, prepared once per
// distinct SQL per connection via the driver's statement cache (prepared:true).

import fs from "node:fs";
import os from "node:os";
import process from "node:process";
import { fileURLToPath } from "node:url";
import {
  Worker,
  isMainThread,
  parentPort,
  workerData,
} from "node:worker_threads";

import mysql from "mysql2/promise";

const C_VAL = "abcdefghij".repeat(12); // 120 chars
const PAD_VAL = "0123456789".repeat(6); // 60 chars

// deadlock, lock wait timeout, duplicate key
const RETRYABLE = new Set([1213, 1205, 1062]);

const SQL_POINT = "SELECT c FROM sbtest WHERE id = ?";
const SQL_RANGE = "SELECT c FROM sbtest WHERE id BETWEEN ? AND ?";
const SQL_SUM = "SELECT SUM(k) FROM sbtest WHERE id BETWEEN ? AND ?";
const SQL_ORDER = "SELECT c FROM sbtest WHERE id BETWEEN ? AND ? ORDER BY c";
const SQL_DISTINCT =
  "SELECT DISTINCT c FROM sbtest WHERE id BETWEEN ? AND ? ORDER BY c";
const SQL_IDX_UPDATE = "UPDATE sbtest SET k = k + 1 WHERE id = ?";
const SQL_NONIDX_UPDATE = "UPDATE sbtest SET c = ? WHERE id = ?";
const SQL_DELETE = "DELETE FROM sbtest WHERE id = ?";
const SQL_INSERT = "INSERT INTO sbtest (id, k, c, pad) VALUES (?, ?, ?, ?)";

// ---------------------------------------------------------------- worker ---

async function workerRun() {
  const d = workerData;

  // Own PRNG stream: 31-bit LCG, state seeded BENCH_SEED + worker index.
  // state * 1103515245 exceeds 2^53, so the step is done in BigInt.
  // Verified (seed 42): rand(100) -> 27, 64, 53, 6, 35.
  let state = BigInt(d.seed + d.index);
  const rand = (n) => {
    state = (state * 1103515245n + 12345n) % 2147483648n;
    return Number(state % BigInt(n));
  };

  const N = d.tableRows;
  const R = d.rangeSize;

  const conn = await mysql.createConnection({
    host: d.host,
    port: d.port,
    user: d.user,
    password: d.password,
    database: d.database,
    // mysql2 runs plaintext unless `ssl` is set — TLS disabled per SPEC.
  });

  let retries = 0;

  // One transaction attempt; values drawn immediately before each statement
  // (draw order = execution order). Every SELECT fetches all rows (mysql2
  // resolves .execute() with the complete row array).
  async function attempt() {
    await conn.query("BEGIN");
    for (let i = 0; i < 10; i++) {
      const [rows] = await conn.execute(SQL_POINT, [1 + rand(N)]);
      void rows.length;
    }
    let lo = 1 + rand(N);
    let [rows] = await conn.execute(SQL_RANGE, [lo, lo + R - 1]);
    void rows.length;
    lo = 1 + rand(N);
    [rows] = await conn.execute(SQL_SUM, [lo, lo + R - 1]);
    void rows[0]; // SUM(k) may arrive as string or null — just read it
    lo = 1 + rand(N);
    [rows] = await conn.execute(SQL_ORDER, [lo, lo + R - 1]);
    void rows.length;
    lo = 1 + rand(N);
    [rows] = await conn.execute(SQL_DISTINCT, [lo, lo + R - 1]);
    void rows.length;
    await conn.execute(SQL_IDX_UPDATE, [1 + rand(N)]);
    await conn.execute(SQL_NONIDX_UPDATE, [C_VAL, 1 + rand(N)]);
    const rowId = 1 + rand(N);
    const k = rand(N);
    await conn.execute(SQL_DELETE, [rowId]);
    await conn.execute(SQL_INSERT, [rowId, k, C_VAL, PAD_VAL]);
    await conn.query("COMMIT");
  }

  // Re-run (with freshly drawn values) until it commits.
  async function txn() {
    for (;;) {
      try {
        await attempt();
        return;
      } catch (err) {
        if (err && RETRYABLE.has(err.errno)) {
          await conn.query("ROLLBACK").catch(() => {}); // ignore rollback errors
          retries++;
          continue;
        }
        throw err;
      }
    }
  }

  for (let j = 0; j < d.warmupTxns; j++) {
    await txn();
  }
  retries = 0; // report retries from the timed window only

  // Barrier: signal ready, wait for start (listener installed before ready
  // is posted so the start message can never be missed).
  const started = new Promise((resolve) => {
    parentPort.on("message", (msg) => {
      if (msg && msg.type === "start") resolve();
    });
  });
  parentPort.postMessage({ type: "ready" });
  await started;

  let latencies;
  if (d.durationS > 0) {
    // Duration mode: transaction count unknown up front — collect latencies
    // in a growable plain array, then convert to a Float64Array so the
    // zero-copy transfer back to main works the same as in events mode.
    // Deadline is checked before each new transaction; a transaction in
    // flight at the deadline runs to successful commit (retries included).
    const collected = [];
    const start = process.hrtime.bigint();
    const deadline = start + BigInt(d.durationS) * 1000000000n;
    while (process.hrtime.bigint() < deadline) {
      const t0 = process.hrtime.bigint();
      await txn();
      collected.push(Number((process.hrtime.bigint() - t0) / 1000n));
    }
    latencies = Float64Array.from(collected);
  } else {
    // Events mode: pre-allocated latency array (integer microseconds,
    // truncated), exactly BENCH_TXNS_PER_THREAD timed transactions.
    latencies = new Float64Array(d.txnsPerThread);
    for (let j = 0; j < d.txnsPerThread; j++) {
      const t0 = process.hrtime.bigint();
      await txn();
      latencies[j] = Number((process.hrtime.bigint() - t0) / 1000n);
    }
  }

  parentPort.postMessage({ type: "done", latencies, retries }, [
    latencies.buffer,
  ]);
  await conn.end();
  parentPort.close(); // drain the event loop so the worker exits (code 0)
}

// ------------------------------------------------------------------ main ---

function getenvStr(name) {
  const v = process.env[name];
  if (v === undefined) {
    console.error(`missing required environment variable: ${name}`);
    process.exit(1);
  }
  return v;
}

function getenvInt(name) {
  const v = getenvStr(name);
  if (!/^-?\d+$/.test(v.trim())) {
    console.error(`invalid integer for environment variable ${name}: ${v}`);
    process.exit(1);
  }
  return parseInt(v.trim(), 10);
}

function getenvIntOpt(name, def) {
  if (process.env[name] === undefined) return def;
  return getenvInt(name);
}

async function main() {
  const cfg = {
    host: getenvStr("MYSQL_HOST"),
    port: getenvInt("MYSQL_PORT"),
    user: getenvStr("MYSQL_USER"),
    password: getenvStr("MYSQL_PASSWORD"),
    database: getenvStr("MYSQL_DB"),
    tableRows: getenvInt("BENCH_TABLE_ROWS"),
    rangeSize: getenvInt("BENCH_RANGE_SIZE"),
    warmupTxns: getenvInt("BENCH_WARMUP_TXNS"),
    txnsPerThread: getenvInt("BENCH_TXNS_PER_THREAD"),
    seed: getenvInt("BENCH_SEED"),
    durationS: getenvIntOpt("BENCH_DURATION_S", 0), // 0 = events mode
  };
  const threads = getenvInt("BENCH_THREADS");
  if (threads < 1 || cfg.txnsPerThread < 1) {
    console.error("BENCH_THREADS and BENCH_TXNS_PER_THREAD must be >= 1");
    process.exit(1);
  }

  const scriptPath = fileURLToPath(import.meta.url);
  const workers = [];
  const exitPromises = [];
  const results = new Array(threads);
  const workerErrors = [];
  let tStart = 0n;
  let tEnd = 0n;

  await new Promise((resolve, reject) => {
    let readyCount = 0;
    let doneCount = 0;
    let spawned = 0;
    // Spawning hundreds of isolates at once exhausts file descriptors while
    // they race through module loading; stagger startup in batches. Startup
    // and warmup are pre-barrier, so the timed window is unaffected.
    const SPAWN_BATCH = 64;

    const spawnOne = (i) => {
      const w = new Worker(scriptPath, {
        workerData: { index: i, ...cfg },
      });
      workers.push(w);
      exitPromises.push(new Promise((res) => w.once("exit", res)));
      w.on("message", (msg) => {
        if (!msg || typeof msg !== "object") return;
        if (msg.type === "ready") {
          readyCount++;
          if (readyCount === spawned && spawned < threads) {
            spawnNextBatch();
          }
          if (readyCount === threads) {
            // All workers connected and warmed up: start the timed window.
            tStart = process.hrtime.bigint();
            for (const ww of workers) ww.postMessage({ type: "start" });
          }
        } else if (msg.type === "done") {
          results[i] = { latencies: msg.latencies, retries: msg.retries };
          doneCount++;
          if (doneCount === threads) {
            tEnd = process.hrtime.bigint();
            resolve();
          }
        } else if (msg.type === "error") {
          workerErrors.push(`worker ${i} error: ${msg.message}`);
          reject(new Error(workerErrors[workerErrors.length - 1]));
        }
      });
      w.on("error", (err) => {
        reject(new Error(`worker ${i} crashed: ${(err && err.stack) || err}`));
      });
      w.on("exit", (code) => {
        if (code !== 0) {
          reject(new Error(`worker ${i} exited with code ${code}`));
        }
      });
    };

    const spawnNextBatch = () => {
      const target = Math.min(spawned + SPAWN_BATCH, threads);
      while (spawned < target) {
        spawnOne(spawned);
        spawned++;
      }
    };

    spawnNextBatch();
  });

  // Workers close their connections and exit after posting "done"
  // (connection teardown stays outside the timed window).
  const exitCodes = await Promise.all(exitPromises);
  if (exitCodes.some((c) => c !== 0)) {
    for (const e of workerErrors) console.error(e);
    console.error("a worker exited non-zero after the timed window");
    process.exit(1);
  }

  const elapsedS = Number(tEnd - tStart) / 1e9;
  // txns = total committed timed transactions actually run. In events mode
  // this equals threads * txnsPerThread; in duration mode each worker ran a
  // variable count, reflected by its latency array length.
  let txns = 0;
  for (const r of results) txns += r.latencies.length;
  const tps = txns / elapsedS;

  const merged = new Float64Array(txns);
  let off = 0;
  let totalRetries = 0;
  for (const r of results) {
    merged.set(r.latencies, off);
    off += r.latencies.length;
    totalRetries += r.retries;
  }
  merged.sort(); // TypedArray sort is numeric ascending
  const n = merged.length;
  const p = (x) => merged[Math.ceil((x / 100) * n) - 1];

  let driverVersion = "unknown";
  try {
    const pkg = new URL("./node_modules/mysql2/package.json", import.meta.url);
    driverVersion = JSON.parse(fs.readFileSync(pkg, "utf8")).version;
  } catch {
    /* leave "unknown" */
  }

  // Manual template so elapsed_s/tps/qps always carry their decimals and the
  // field order matches SPEC.md exactly.
  const line =
    `{"lang":"javascript","driver":"mysql2"` +
    `,"driver_version":${JSON.stringify(driverVersion)}` +
    `,"runtime_version":${JSON.stringify(process.version)}` +
    `,"prepared":true` +
    `,"client_host":${JSON.stringify(os.hostname())}` +
    `,"db_host":${JSON.stringify(cfg.host)}` +
    `,"threads":${threads}` +
    `,"txns":${txns}` +
    `,"retries":${totalRetries}` +
    `,"errors":0` +
    `,"elapsed_s":${elapsedS.toFixed(3)}` +
    `,"tps":${tps.toFixed(1)}` +
    `,"qps":${(tps * 20).toFixed(1)}` +
    `,"p50_us":${p(50)}` +
    `,"p95_us":${p(95)}` +
    `,"p99_us":${p(99)}` +
    `,"max_us":${merged[n - 1]}}`;
  console.log(line);
}

if (isMainThread) {
  main().catch((err) => {
    console.error((err && err.stack) || String(err));
    process.exit(1);
  });
} else {
  workerRun().catch((err) => {
    try {
      parentPort.postMessage({
        type: "error",
        message: (err && err.stack) || String(err),
      });
    } catch {
      /* parent port already gone */
    }
    process.exit(1); // stops this worker thread only; main reports and exits 1
  });
}
