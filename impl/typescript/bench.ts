// MySQL language benchmark - TypeScript implementation (Node.js + mysql2).
//
// A strictly-typed port of the v3 JavaScript implementation, following SPEC.md
// v3 exactly: one of four micro-workloads selected by BENCH_WORKLOAD (1..4),
// run with BENCH_THREADS concurrent workers, one JSON result line on stdout.
//
//   WL1 int-insert   prepared multi-row INSERT (BENCH_INT_BATCH placeholders)
//   WL2 int-read     SELECT val ... LIMIT, sum in app (64-bit accumulator)
//   WL3 blob-insert  random blob + app-side MD5, prepared INSERT(id, data)
//   WL4 blob-read    SELECT data WHERE id = ?, app-side MD5
//
// Each benchmark worker is a worker_threads Worker (real OS thread, own V8
// isolate) with its own mysql2 connection (no pools; maxAllowedPacket set to
// 256 MiB for 1 MiB blobs), its own 31-bit LCG PRNG seeded BENCH_SEED +
// worker_index, barrier-based timing coordinated over postMessage, and
// per-operation latency in integer microseconds. Workers publish their
// completed-op counts into a shared Int32Array slot so main can checkpoint
// progress to BENCH_PROGRESS_FILE while the timed phase runs.
//
// mysql2's .execute() is a server-side prepared statement, prepared once per
// distinct SQL per connection via the driver's statement cache (prepared:true).
//
// Compiled by tsc into dist/bench.js; new Worker(fileURLToPath(import.meta.url))
// therefore resolves to the running compiled file, as in the JS version.

import { createHash, randomFillSync } from "node:crypto";
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

import { createConnection } from "mysql2/promise";
import type { ConnectionOptions, RowDataPacket } from "mysql2/promise";

// 256 MiB — big enough to send 1 MiB (and larger) blobs as a single packet.
const MAX_ALLOWED_PACKET = 268435456;

// ----------------------------------------------------------------- types ---

interface BenchConfig {
  host: string;
  port: number;
  user: string;
  password: string;
  database: string;
  workload: number;
  seed: number;
  intRows: number;
  intBatch: number;
  blobCount: number;
  blobSize: number;
}

interface BenchWorkerData extends BenchConfig {
  index: number;
  // Per-worker completed-op counters (one Int32 slot per worker). Each worker
  // writes only its own slot; main reads all slots for progress checkpoints.
  progressBuf: SharedArrayBuffer;
}

interface WorkloadMeta {
  name: string;
  unit: string;
  blob: boolean;
}

function workloadMeta(w: number): WorkloadMeta {
  switch (w) {
    case 1:
      return { name: "int-insert", unit: "rows/s", blob: false };
    case 2:
      return { name: "int-read", unit: "rows/s", blob: false };
    case 3:
      return { name: "blob-insert", unit: "blobs/s", blob: true };
    case 4:
      return { name: "blob-read", unit: "blobs/s", blob: true };
    default:
      console.error(`invalid BENCH_WORKLOAD: ${w} (must be 1..4)`);
      process.exit(1);
  }
}

type MainToWorkerMsg = { type: "start" };

type WorkerToMainMsg =
  | { type: "ready" }
  | { type: "done"; latencies: Float64Array; ops: number }
  | { type: "error"; message: string };

interface WorkerResult {
  latencies: Float64Array;
  ops: number;
}

function errText(err: unknown): string {
  return err instanceof Error && err.stack ? err.stack : String(err);
}

// ---------------------------------------------------------------- worker ---

async function workerRun(): Promise<void> {
  const port = parentPort;
  if (port === null) throw new Error("workerRun called without a parent port");
  const d = workerData as BenchWorkerData;

  // Own PRNG stream: 31-bit LCG, state seeded BENCH_SEED + worker index.
  // state * 1103515245 exceeds 2^53, so the step is done in BigInt; the
  // returned state is < 2^31 and so fits exactly in a JS number.
  // (Validation, seed 42 worker 0: first next() = 1250496027.)
  let state = BigInt(d.seed + d.index);
  const next = (): number => {
    state = (state * 1103515245n + 12345n) % 2147483648n;
    return Number(state);
  };

  const progress = new Int32Array(d.progressBuf);
  const publish = (n: number): void => {
    Atomics.store(progress, d.index, n);
  };

  const connOpts: ConnectionOptions & { maxAllowedPacket?: number } = {
    host: d.host,
    port: d.port,
    user: d.user,
    password: d.password,
    database: d.database,
    // mysql2 runs plaintext unless `ssl` is set — TLS disabled per SPEC.
    maxAllowedPacket: MAX_ALLOWED_PACKET,
  };
  const conn = await createConnection(connOpts);

  // Black-box sink so app-side sums / MD5 digests are not optimized away.
  let sink = 0;

  // One latency sample per operation, integer microseconds (truncated via
  // BigInt division). Sizing and the timed body depend on the workload.
  let latencies: Float64Array = new Float64Array(0);
  let ops = 0;

  const runWorkload = async (): Promise<void> => {
    switch (d.workload) {
      case 1: {
        // Integer insert: one prepared multi-row INSERT of intBatch rows,
        // executed intRows / intBatch times. Autocommit ON — each INSERT is
        // its own commit; no wrapping BEGIN/COMMIT (SPEC, identical per lang).
        const batch = d.intBatch;
        const stmts = Math.floor(d.intRows / batch);
        const sql =
          "INSERT INTO bench_int (val) VALUES " +
          new Array(batch).fill("(?)").join(",");
        latencies = new Float64Array(stmts);
        ops = stmts * batch;
        const params = new Array<number>(batch);
        let done = 0;
        for (let s = 0; s < stmts; s++) {
          for (let j = 0; j < batch; j++) params[j] = next() | 0; // signed int32
          const t0 = process.hrtime.bigint();
          await conn.execute(sql, params);
          latencies[s] = Number((process.hrtime.bigint() - t0) / 1000n);
          done += batch;
          publish(done);
        }
        break;
      }
      case 2: {
        // Integer fetch + sum: one scan per worker, sum val in app code.
        const sql = `SELECT val FROM bench_int LIMIT ${d.intRows}`;
        latencies = new Float64Array(1);
        const t0 = process.hrtime.bigint();
        const [rows] = await conn.execute<RowDataPacket[]>(sql);
        let sum = 0; // 64-bit accumulator (double is exact for these sums)
        for (const row of rows) sum += Number(row.val);
        latencies[0] = Number((process.hrtime.bigint() - t0) / 1000n);
        sink += sum;
        ops = rows.length;
        publish(ops);
        break;
      }
      case 3: {
        // Blob insert + MD5. Fill the buffer once per worker with a fast
        // native CSPRNG (not a per-byte interpreted loop), then before each
        // blob overwrite its first 8 bytes with the blob id so every blob's
        // content (and MD5) differs. MD5 hashes the full buffer each time.
        // Autocommit ON — each INSERT commits on its own (SPEC). Latency = the
        // INSERT.
        const count = d.blobCount;
        const sql = "INSERT INTO bench_blob (id, data) VALUES (?, ?)";
        latencies = new Float64Array(count);
        ops = count;
        const buf = Buffer.allocUnsafe(d.blobSize);
        randomFillSync(buf); // once per worker
        for (let k = 0; k < count; k++) {
          const id = d.index * count + k;
          buf.writeBigUInt64LE(BigInt(id), 0); // first 8 bytes = blob id
          sink += createHash("md5").update(buf).digest("hex").charCodeAt(0);
          const t0 = process.hrtime.bigint();
          await conn.execute(sql, [id, buf]);
          latencies[k] = Number((process.hrtime.bigint() - t0) / 1000n);
          publish(k + 1);
        }
        break;
      }
      case 4: {
        // Blob read + MD5: read own blobs by id range, MD5 each in app.
        // Latency = the SELECT (MD5 computed outside the sample, per SPEC).
        const count = d.blobCount;
        const sql = "SELECT data FROM bench_blob WHERE id = ?";
        latencies = new Float64Array(count);
        ops = count;
        for (let k = 0; k < count; k++) {
          const id = d.index * count + k;
          const t0 = process.hrtime.bigint();
          const [rows] = await conn.execute<RowDataPacket[]>(sql, [id]);
          latencies[k] = Number((process.hrtime.bigint() - t0) / 1000n);
          const data = rows[0].data as Buffer;
          sink += createHash("md5").update(data).digest("hex").charCodeAt(0);
          publish(k + 1);
        }
        break;
      }
      default:
        throw new Error(`invalid BENCH_WORKLOAD: ${d.workload}`);
    }
  };

  // Barrier: signal ready, wait for start (listener installed before ready
  // is posted so the start message can never be missed). Connection and
  // statement preparation (first .execute per SQL) stay outside the window;
  // the first prepare happens inside the timed loop, matching the JS impl —
  // it is a one-time cost per worker and negligible against the full load.
  const started = new Promise<void>((resolve) => {
    port.on("message", (msg: unknown) => {
      if (
        msg !== null &&
        typeof msg === "object" &&
        (msg as MainToWorkerMsg).type === "start"
      ) {
        resolve();
      }
    });
  });
  const readyMsg: WorkerToMainMsg = { type: "ready" };
  port.postMessage(readyMsg);
  await started;

  await runWorkload();

  // Force the sink to be observed so the sums / digests cannot be elided.
  if (!Number.isFinite(sink)) throw new Error("sink not finite");

  const doneMsg: WorkerToMainMsg = { type: "done", latencies, ops };
  port.postMessage(doneMsg, [latencies.buffer as ArrayBuffer]);
  await conn.end();
  port.close(); // drain the event loop so the worker exits (code 0)
}

// ------------------------------------------------------------------ main ---

function getenvStr(name: string): string {
  const v = process.env[name];
  if (v === undefined) {
    console.error(`missing required environment variable: ${name}`);
    process.exit(1);
  }
  return v;
}

function getenvInt(name: string): number {
  const v = getenvStr(name);
  if (!/^-?\d+$/.test(v.trim())) {
    console.error(`invalid integer for environment variable ${name}: ${v}`);
    process.exit(1);
  }
  return parseInt(v.trim(), 10);
}

// Optional integer environment variable: defaults when unset, but a value
// that is present must still be a valid integer.
function getenvIntOpt(name: string, def: number): number {
  if (process.env[name] === undefined || process.env[name] === "") return def;
  return getenvInt(name);
}

// Version of an installed package, read at runtime. The compiled benchmark
// runs from dist/, so node_modules lives one level up from import.meta.url.
function readPkgVersion(relPath: string): string {
  try {
    const pkg = new URL(relPath, import.meta.url);
    const parsed = JSON.parse(fs.readFileSync(pkg, "utf8")) as {
      version?: unknown;
    };
    return typeof parsed.version === "string" ? parsed.version : "unknown";
  } catch {
    return "unknown";
  }
}

async function main(): Promise<void> {
  const cfg: BenchConfig = {
    host: getenvStr("MYSQL_HOST"),
    port: getenvInt("MYSQL_PORT"),
    user: getenvStr("MYSQL_USER"),
    password: getenvStr("MYSQL_PASSWORD"),
    database: getenvStr("MYSQL_DB"),
    workload: getenvInt("BENCH_WORKLOAD"),
    seed: getenvInt("BENCH_SEED"),
    intRows: getenvIntOpt("BENCH_INT_ROWS", 1000000),
    intBatch: getenvIntOpt("BENCH_INT_BATCH", 10),
    blobCount: getenvIntOpt("BENCH_BLOB_COUNT", 100),
    blobSize: getenvIntOpt("BENCH_BLOB_SIZE", 1048576),
  };
  const threads = getenvInt("BENCH_THREADS");
  const meta = workloadMeta(cfg.workload);

  if (threads < 1) {
    console.error("BENCH_THREADS must be >= 1");
    process.exit(1);
  }
  if (!meta.blob && (cfg.intRows < 1 || cfg.intBatch < 1)) {
    console.error("BENCH_INT_ROWS and BENCH_INT_BATCH must be >= 1");
    process.exit(1);
  }
  if (meta.blob && (cfg.blobCount < 1 || cfg.blobSize < 1)) {
    console.error("BENCH_BLOB_COUNT and BENCH_BLOB_SIZE must be >= 1");
    process.exit(1);
  }

  // Progress checkpoint (kill / long-run recovery). Skipped when unset/empty.
  const progressFile = process.env.BENCH_PROGRESS_FILE ?? "";
  const progressSec = getenvIntOpt("BENCH_PROGRESS_SEC", 5);

  const progressBuf = new SharedArrayBuffer(threads * 4);
  const progress = new Int32Array(progressBuf);

  const scriptPath = fileURLToPath(import.meta.url);
  const workers: Worker[] = [];
  const exitPromises: Promise<number>[] = [];
  const results: (WorkerResult | undefined)[] = new Array(threads);
  const workerErrors: string[] = [];
  let tStart = 0n;
  let tEnd = 0n;
  let progressTimer: NodeJS.Timeout | undefined;

  const writeProgress = (): void => {
    let ops = 0;
    for (let i = 0; i < threads; i++) ops += Atomics.load(progress, i);
    const elapsed = Number(process.hrtime.bigint() - tStart) / 1e9;
    const throughput = elapsed > 0 ? ops / elapsed : 0;
    const line = JSON.stringify({
      workload: cfg.workload,
      workload_name: meta.name,
      unit: meta.unit,
      threads,
      ops,
      elapsed_s: Number(elapsed.toFixed(3)),
      throughput: Number(throughput.toFixed(1)),
      partial: true,
    });
    const tmp = `${progressFile}.tmp`;
    try {
      fs.writeFileSync(tmp, line + "\n");
      fs.renameSync(tmp, progressFile); // atomic replace over FILE
    } catch (err) {
      console.error(`progress checkpoint failed: ${errText(err)}`);
    }
  };

  await new Promise<void>((resolve, reject) => {
    let readyCount = 0;
    let doneCount = 0;
    let spawned = 0;
    // Spawning hundreds of isolates at once exhausts file descriptors while
    // they race through module loading; stagger startup in batches. Startup
    // is pre-barrier, so the timed window is unaffected.
    const SPAWN_BATCH = 64;

    const spawnOne = (i: number): void => {
      const wd: BenchWorkerData = { index: i, progressBuf, ...cfg };
      const w = new Worker(scriptPath, { workerData: wd });
      workers.push(w);
      exitPromises.push(new Promise<number>((res) => w.once("exit", res)));
      w.on("message", (raw: unknown) => {
        if (raw === null || typeof raw !== "object") return;
        const msg = raw as WorkerToMainMsg;
        if (msg.type === "ready") {
          readyCount++;
          if (readyCount === spawned && spawned < threads) {
            spawnNextBatch();
          }
          if (readyCount === threads) {
            // All workers connected: start the timed window and the progress
            // checkpoint timer.
            tStart = process.hrtime.bigint();
            if (progressFile !== "" && progressSec > 0) {
              progressTimer = setInterval(writeProgress, progressSec * 1000);
            }
            const startMsg: MainToWorkerMsg = { type: "start" };
            for (const ww of workers) ww.postMessage(startMsg);
          }
        } else if (msg.type === "done") {
          results[i] = { latencies: msg.latencies, ops: msg.ops };
          doneCount++;
          if (doneCount === threads) {
            tEnd = process.hrtime.bigint();
            if (progressTimer !== undefined) clearInterval(progressTimer);
            resolve();
          }
        } else if (msg.type === "error") {
          workerErrors.push(`worker ${i} error: ${msg.message}`);
          reject(new Error(workerErrors[workerErrors.length - 1]));
        }
      });
      w.on("error", (err: Error) => {
        reject(new Error(`worker ${i} crashed: ${errText(err)}`));
      });
      w.on("exit", (code: number) => {
        if (code !== 0) {
          reject(new Error(`worker ${i} exited with code ${code}`));
        }
      });
    };

    const spawnNextBatch = (): void => {
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

  // ops = total operations across workers (rows for WL1/2, blobs for WL3/4).
  const collected: WorkerResult[] = [];
  let ops = 0;
  for (const r of results) {
    if (r === undefined) {
      console.error("internal error: missing worker result");
      process.exit(1);
    }
    collected.push(r);
    ops += r.ops;
  }
  const throughput = ops / elapsedS;
  const mbPerS = meta.blob ? (ops * cfg.blobSize) / 1e6 / elapsedS : 0;

  // Merge, sort, percentiles: p(x) = arr[ceil(x/100 * n) - 1], max = arr[n-1].
  let total = 0;
  for (const r of collected) total += r.latencies.length;
  const merged = new Float64Array(total);
  let off = 0;
  for (const r of collected) {
    merged.set(r.latencies, off);
    off += r.latencies.length;
  }
  merged.sort(); // TypedArray sort is numeric ascending
  const n = merged.length;
  const p = (x: number): number =>
    n === 0 ? 0 : merged[Math.ceil((x / 100) * n) - 1];
  const maxUs = n === 0 ? 0 : merged[n - 1];

  const driverVersion = readPkgVersion("../node_modules/mysql2/package.json");
  const tscVersion = readPkgVersion("../node_modules/typescript/package.json");
  const runtimeVersion = `node ${process.version} / tsc ${tscVersion}`;

  // Manual template so throughput/mb_per_s/elapsed_s always carry their
  // decimals and the field order matches SPEC.md exactly.
  const line =
    `{"lang":"typescript"` +
    `,"workload":${cfg.workload}` +
    `,"workload_name":${JSON.stringify(meta.name)}` +
    `,"unit":${JSON.stringify(meta.unit)}` +
    `,"driver":"mysql2"` +
    `,"driver_version":${JSON.stringify(driverVersion)}` +
    `,"runtime_version":${JSON.stringify(runtimeVersion)}` +
    `,"prepared":true` +
    `,"client_host":${JSON.stringify(os.hostname())}` +
    `,"db_host":${JSON.stringify(cfg.host)}` +
    `,"threads":${threads}` +
    `,"ops":${ops}` +
    `,"errors":0` +
    `,"elapsed_s":${elapsedS.toFixed(3)}` +
    `,"throughput":${throughput.toFixed(1)}` +
    `,"mb_per_s":${meta.blob ? mbPerS.toFixed(1) : "0"}` +
    `,"p50_us":${p(50)}` +
    `,"p95_us":${p(95)}` +
    `,"p99_us":${p(99)}` +
    `,"max_us":${maxUs}` +
    `,"client_cpu_s":0,"server_cpu_s":0}`;
  console.log(line);

  // Final JSON is on stdout now; the progress file's data is superseded.
  if (progressFile !== "") {
    try {
      fs.unlinkSync(progressFile);
    } catch {
      /* already gone — fine */
    }
  }
}

if (isMainThread) {
  main().catch((err: unknown) => {
    console.error(errText(err));
    process.exit(1);
  });
} else {
  workerRun().catch((err: unknown) => {
    try {
      const errMsg: WorkerToMainMsg = { type: "error", message: errText(err) };
      parentPort?.postMessage(errMsg);
    } catch {
      /* parent port already gone */
    }
    process.exit(1); // stops this worker thread only; main reports and exits 1
  });
}
