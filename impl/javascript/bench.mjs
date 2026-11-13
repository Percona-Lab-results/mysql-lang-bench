// MySQL language benchmark - JavaScript implementation (Node.js + mysql2).
//
// Follows SPEC.md v3 exactly: four micro-workloads selected by BENCH_WORKLOAD
// (1..4), BENCH_THREADS concurrent workers, one JSON line on stdout.
//
//   1 int-insert   prepared multi-row INSERT into bench_int (BENCH_INT_BATCH
//                  rows/stmt, BENCH_INT_ROWS rows total per worker)
//   2 int-fetch    SELECT val FROM bench_int LIMIT N, sum val in app code
//   3 blob-insert  one native random BENCH_BLOB_SIZE buffer per worker, its
//                  first 8 bytes stamped with the blob id, MD5 in app code,
//                  INSERT INTO bench_blob (id, data)
//   4 blob-fetch   SELECT data WHERE id = ?, MD5 each blob in app code
//
// One worker_threads Worker per benchmark worker (real OS thread, own V8
// isolate), each with its own mysql2 connection (no pools). Barrier-based
// timing coordinated over postMessage; process.hrtime.bigint() timing; per-
// operation latency in integer microseconds (truncated) into a per-worker
// Float64Array transferred zero-copy back to main.
//
// mysql2's prepared statements are server-side; each worker prepares its
// statement before the start barrier (outside the timed window), so prepared
// is reported true.
//
// Intermediate progress: each worker publishes its completed-op count into a
// per-worker slot of a shared Int32Array (SharedArrayBuffer, lock-free). When
// BENCH_PROGRESS_FILE is set, the main thread samples the slots every
// BENCH_PROGRESS_SEC seconds and atomically overwrites the file (tmp + rename)
// with a partial JSON line; it is deleted on successful completion.

import crypto from "node:crypto";
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

// 256 MiB — a 1 MiB blob plus the prepared-statement wire framing must fit.
const MAX_ALLOWED_PACKET = 268435456;

const WL_META = {
  1: { name: "int-insert", unit: "rows/s" },
  2: { name: "int-fetch", unit: "rows/s" },
  3: { name: "blob-insert", unit: "blobs/s" },
  4: { name: "blob-fetch", unit: "blobs/s" },
};

// ----------------------------------------------------------------- PRNG ---
//
// 31-bit LCG, state seeded BENCH_SEED + worker index:
//   next(): state = (state*1103515245 + 12345) mod 2^31; return state
//
// state*1103515245 overflows 2^53, but the result mod 2^31 depends only on the
// low 32 bits of the product, so Math.imul (exact low-32-bit signed multiply)
// gives it without BigInt:
//   p mod 2^31 = ((imul(state, A) >>> 0) + C) & 0x7FFFFFFF
// `& 0x7FFFFFFF` applies ToInt32 (mod 2^32) then masks the low 31 bits, which
// equals mod 2^31 for any value below ~2^32. Verified (seed 42, worker 0):
// first next() = 1250496027, matching SPEC. BigInt would make the per-byte
// blob fill (workload 3) pathologically slow; this stays fast and exact.
function makeNext(seed) {
  let state = seed >>> 0;
  return function next() {
    state = ((Math.imul(state, 1103515245) >>> 0) + 12345) & 0x7fffffff;
    return state;
  };
}

// ---------------------------------------------------------------- worker ---

async function workerRun() {
  const d = workerData;
  const next = makeNext(d.seed + d.index);

  // Per-worker progress slot: this worker is the sole writer (lock-free).
  const progress = new Int32Array(d.progressSab);
  const publish = (done) => Atomics.store(progress, d.index, done);

  const conn = await mysql.createConnection({
    host: d.host,
    port: d.port,
    user: d.user,
    password: d.password,
    database: d.database,
    maxAllowedPacket: MAX_ALLOWED_PACKET,
    // Workload 2 reads rows as plain arrays ([val]) to limit per-row object
    // overhead on a 1M-row buffered result.
    rowsAsArray: d.workload === 2,
    // mysql2 runs plaintext unless `ssl` is set — TLS disabled per SPEC.
  });

  // Prepare the statement(s) for this workload *before* the barrier so the
  // prepare round-trip stays outside the timed window.
  let stmt = null;
  if (d.workload === 1) {
    const sql =
      "INSERT INTO bench_int (val) VALUES " +
      Array(d.intBatch).fill("(?)").join(",");
    stmt = await conn.prepare(sql);
  } else if (d.workload === 2) {
    // LIMIT must be a literal for a server-side prepared statement in MySQL;
    // the row count is a trusted integer env var, so inline it.
    stmt = await conn.prepare(
      `SELECT val FROM bench_int LIMIT ${d.intRows}`,
    );
  } else if (d.workload === 3) {
    stmt = await conn.prepare(
      "INSERT INTO bench_blob (id, data) VALUES (?, ?)",
    );
  } else if (d.workload === 4) {
    stmt = await conn.prepare("SELECT data FROM bench_blob WHERE id = ?");
  } else {
    throw new Error(`invalid BENCH_WORKLOAD: ${d.workload}`);
  }

  // Barrier: install the start listener, signal ready, wait for start.
  const started = new Promise((resolve) => {
    parentPort.on("message", (msg) => {
      if (msg && msg.type === "start") resolve();
    });
  });
  parentPort.postMessage({ type: "ready" });
  await started;

  let latencies;
  let sink = 0; // black-box accumulator so hashing/summing is not elided

  if (d.workload === 1) {
    // One prepared multi-row INSERT of intBatch rows, run intRows/intBatch
    // times. Each bound value is next() as a signed 32-bit int (next() is
    // 31-bit so already a non-negative signed int32).
    const batches = Math.floor(d.intRows / d.intBatch);
    latencies = new Float64Array(batches);
    const params = new Array(d.intBatch);
    let done = 0;
    for (let b = 0; b < batches; b++) {
      for (let j = 0; j < d.intBatch; j++) params[j] = next() | 0;
      const t0 = process.hrtime.bigint();
      await stmt.execute(params);
      latencies[b] = Number((process.hrtime.bigint() - t0) / 1000n);
      done += d.intBatch;
      publish(done);
    }
  } else if (d.workload === 2) {
    // Single scan; sum val (rowsAsArray => row[0]) into a 64-bit-safe sink.
    // Max per-worker sum ~1M * 2^31 ≈ 2.1e15 < 2^53, exact in a JS number.
    latencies = new Float64Array(1);
    const t0 = process.hrtime.bigint();
    const [rows] = await stmt.execute([]);
    let acc = 0;
    for (let r = 0; r < rows.length; r++) acc += rows[r][0];
    latencies[0] = Number((process.hrtime.bigint() - t0) / 1000n);
    sink += acc;
    publish(rows.length);
  } else if (d.workload === 3) {
    // Fill the buffer ONCE per worker with fast native random bytes (SPEC:
    // content generation must be cheap and comparable, NOT a per-byte
    // interpreted loop). Before each blob, overwrite the first 8 bytes with
    // the blob id so every blob's content — and its MD5 — differs; MD5 still
    // hashes the full BENCH_BLOB_SIZE buffer. Autocommit: each INSERT commits
    // on its own (no wrapping transaction). id = index*BLOB_COUNT + k
    // (contiguous per-worker range).
    latencies = new Float64Array(d.blobCount);
    const buf = Buffer.allocUnsafe(d.blobSize);
    crypto.randomFillSync(buf);
    const base = d.index * d.blobCount;
    for (let k = 0; k < d.blobCount; k++) {
      buf.writeBigUInt64LE(BigInt(base + k), 0);
      const digest = crypto.createHash("md5").update(buf).digest("hex");
      sink += digest.charCodeAt(0);
      const t0 = process.hrtime.bigint();
      await stmt.execute([base + k, buf]);
      latencies[k] = Number((process.hrtime.bigint() - t0) / 1000n);
      publish(k + 1);
    }
  } else {
    // Workload 4: read this worker's own blobs by id, MD5 each in app code.
    latencies = new Float64Array(d.blobCount);
    const base = d.index * d.blobCount;
    for (let k = 0; k < d.blobCount; k++) {
      const t0 = process.hrtime.bigint();
      const [rows] = await stmt.execute([base + k]);
      latencies[k] = Number((process.hrtime.bigint() - t0) / 1000n);
      const data = rows[0].data; // LONGBLOB -> Buffer
      const digest = crypto.createHash("md5").update(data).digest("hex");
      sink += digest.charCodeAt(0);
      publish(k + 1);
    }
  }

  // `sink` rides along so V8 cannot treat the hashing/summing as dead code.
  parentPort.postMessage({ type: "done", latencies, sink }, [
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
  if (process.env[name] === undefined || process.env[name] === "") return def;
  return getenvInt(name);
}

async function main() {
  const workload = getenvInt("BENCH_WORKLOAD");
  if (!WL_META[workload]) {
    console.error(`BENCH_WORKLOAD must be 1..4, got ${workload}`);
    process.exit(1);
  }
  const meta = WL_META[workload];

  const cfg = {
    workload,
    host: getenvStr("MYSQL_HOST"),
    port: getenvInt("MYSQL_PORT"),
    user: getenvStr("MYSQL_USER"),
    password: getenvStr("MYSQL_PASSWORD"),
    database: getenvStr("MYSQL_DB"),
    seed: getenvInt("BENCH_SEED"),
    intRows: getenvIntOpt("BENCH_INT_ROWS", 1000000),
    intBatch: getenvIntOpt("BENCH_INT_BATCH", 10),
    blobCount: getenvIntOpt("BENCH_BLOB_COUNT", 100),
    blobSize: getenvIntOpt("BENCH_BLOB_SIZE", 1048576),
  };
  const threads = getenvInt("BENCH_THREADS");
  if (threads < 1) {
    console.error("BENCH_THREADS must be >= 1");
    process.exit(1);
  }
  if ((workload === 1 || workload === 2) && cfg.intRows < 1) {
    console.error("BENCH_INT_ROWS must be >= 1");
    process.exit(1);
  }
  if (workload === 1 && cfg.intBatch < 1) {
    console.error("BENCH_INT_BATCH must be >= 1");
    process.exit(1);
  }
  if ((workload === 3 || workload === 4) && cfg.blobCount < 1) {
    console.error("BENCH_BLOB_COUNT must be >= 1");
    process.exit(1);
  }

  // Rows/blobs each worker completes, and total ops across workers.
  const perWorker =
    workload === 1
      ? Math.floor(cfg.intRows / cfg.intBatch) * cfg.intBatch
      : workload === 2
        ? cfg.intRows
        : cfg.blobCount;
  const ops = threads * perWorker;

  const progressFile = process.env.BENCH_PROGRESS_FILE || "";
  const progressSec = getenvIntOpt("BENCH_PROGRESS_SEC", 5);

  // Shared per-worker completed-op counters (one Int32 slot each).
  const progressSab = new SharedArrayBuffer(threads * 4);
  const progressArr = new Int32Array(progressSab);

  const scriptPath = fileURLToPath(import.meta.url);
  const workers = [];
  const exitPromises = [];
  const results = new Array(threads);
  const workerErrors = [];
  let tStart = 0n;
  let tEnd = 0n;
  let progressTimer = null;

  const writeProgress = () => {
    let done = 0;
    for (let i = 0; i < threads; i++) done += Atomics.load(progressArr, i);
    const elapsed = Number(process.hrtime.bigint() - tStart) / 1e9;
    const tput = elapsed > 0 ? done / elapsed : 0;
    const line =
      `{"workload":${workload}` +
      `,"workload_name":${JSON.stringify(meta.name)}` +
      `,"unit":${JSON.stringify(meta.unit)}` +
      `,"threads":${threads}` +
      `,"ops":${done}` +
      `,"elapsed_s":${elapsed.toFixed(3)}` +
      `,"throughput":${tput.toFixed(1)}` +
      `,"partial":true}`;
    try {
      const tmp = progressFile + ".tmp";
      fs.writeFileSync(tmp, line + "\n");
      fs.renameSync(tmp, progressFile);
    } catch {
      /* progress is best-effort; never fail the run over it */
    }
  };

  await new Promise((resolve, reject) => {
    let readyCount = 0;
    let doneCount = 0;
    let spawned = 0;
    // Spawning hundreds of isolates at once exhausts file descriptors while
    // they race through module loading; stagger startup in batches. Startup
    // and prepare are pre-barrier, so the timed window is unaffected.
    const SPAWN_BATCH = 64;

    const spawnOne = (i) => {
      const w = new Worker(scriptPath, {
        workerData: { index: i, progressSab, ...cfg },
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
            // All workers connected and prepared: start the timed window.
            tStart = process.hrtime.bigint();
            if (progressFile) {
              progressTimer = setInterval(writeProgress, progressSec * 1000);
            }
            for (const ww of workers) ww.postMessage({ type: "start" });
          }
        } else if (msg.type === "done") {
          results[i] = { latencies: msg.latencies };
          doneCount++;
          if (doneCount === threads) {
            tEnd = process.hrtime.bigint();
            if (progressTimer) clearInterval(progressTimer);
            resolve();
          }
        } else if (msg.type === "error") {
          workerErrors.push(`worker ${i} error: ${msg.message}`);
          if (progressTimer) clearInterval(progressTimer);
          reject(new Error(workerErrors[workerErrors.length - 1]));
        }
      });
      w.on("error", (err) => {
        if (progressTimer) clearInterval(progressTimer);
        reject(new Error(`worker ${i} crashed: ${(err && err.stack) || err}`));
      });
      w.on("exit", (code) => {
        if (code !== 0) {
          if (progressTimer) clearInterval(progressTimer);
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
  const throughput = ops / elapsedS;
  const mbPerS =
    workload === 3 || workload === 4
      ? (ops * cfg.blobSize) / 1e6 / elapsedS
      : 0;

  // Merge per-worker latency samples, sort, compute percentiles.
  let n = 0;
  for (const r of results) n += r.latencies.length;
  const merged = new Float64Array(n);
  let off = 0;
  for (const r of results) {
    merged.set(r.latencies, off);
    off += r.latencies.length;
  }
  merged.sort(); // TypedArray sort is numeric ascending
  const p = (x) => merged[Math.ceil((x / 100) * n) - 1];

  let driverVersion = "unknown";
  try {
    const pkg = new URL("./node_modules/mysql2/package.json", import.meta.url);
    driverVersion = JSON.parse(fs.readFileSync(pkg, "utf8")).version;
  } catch {
    /* leave "unknown" */
  }

  // Manual template so throughput/mb_per_s/elapsed_s always carry their
  // decimals and the field order matches SPEC.md exactly.
  const line =
    `{"lang":"javascript"` +
    `,"workload":${workload}` +
    `,"workload_name":${JSON.stringify(meta.name)}` +
    `,"unit":${JSON.stringify(meta.unit)}` +
    `,"driver":"mysql2"` +
    `,"driver_version":${JSON.stringify(driverVersion)}` +
    `,"runtime_version":${JSON.stringify(process.version)}` +
    `,"prepared":true` +
    `,"client_host":${JSON.stringify(os.hostname())}` +
    `,"db_host":${JSON.stringify(cfg.host)}` +
    `,"threads":${threads}` +
    `,"ops":${ops}` +
    `,"errors":0` +
    `,"elapsed_s":${elapsedS.toFixed(3)}` +
    `,"throughput":${throughput.toFixed(1)}` +
    `,"mb_per_s":${workload === 3 || workload === 4 ? mbPerS.toFixed(1) : 0}` +
    `,"p50_us":${p(50)}` +
    `,"p95_us":${p(95)}` +
    `,"p99_us":${p(99)}` +
    `,"max_us":${merged[n - 1]}` +
    `,"client_cpu_s":0,"server_cpu_s":0}`;
  console.log(line);

  // Success: the result is on stdout, so the partial file is superseded.
  if (progressFile) {
    try {
      fs.unlinkSync(progressFile);
    } catch {
      /* already gone */
    }
  }
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
