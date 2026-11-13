// MySQL language benchmark — Rust implementation (SPEC.md v3).
//
// One of four micro-workloads, selected by BENCH_WORKLOAD (1..4), run by
// BENCH_THREADS worker threads. Each worker owns one TCP connection and its
// own PRNG stream; every worker performs the full per-worker amount (work
// scales with worker count — nothing is shared/divided). Timing is
// barrier-based: workers connect + prepare, wait at a start barrier, main
// releases it and samples a monotonic clock, joins, samples again.
//
//   WL1 int-insert  : prepared multi-row INSERT (BENCH_INT_BATCH placeholders)
//   WL2 int-scan    : SELECT val ... LIMIT n, sum in app code (i64)
//   WL3 blob-insert : generate BENCH_BLOB_SIZE bytes, MD5, INSERT blob
//   WL4 blob-read   : SELECT data WHERE id=?, MD5 each blob
//
// Intermediate progress: when BENCH_PROGRESS_FILE is set, each worker bumps a
// per-worker atomic slot as operations complete; a monitor thread sums the
// slots every BENCH_PROGRESS_SEC seconds and atomically overwrites the file
// (tmp + rename) with one partial JSON line. The file is deleted on success.

use std::fs;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Barrier;
use std::thread;
use std::time::{Duration, Instant};

use md5::{Digest, Md5};
use mysql::prelude::Queryable;
use mysql::{Conn, OptsBuilder, Params, Value};

/// Must match the exact version pinned in Cargo.toml.
const DRIVER_VERSION: &str = "25.0.1";

/// 31-bit LCG per SPEC.md. State and products fit in u64 (state < 2^31,
/// 2^31 * 1103515245 < 2^62), so plain u64 arithmetic is exact.
/// Validation (seed 42, worker 0): first next() = 1250496027.
struct Lcg {
    state: u64,
}

impl Lcg {
    fn new(seed: u64) -> Self {
        Lcg { state: seed }
    }

    #[inline]
    fn next(&mut self) -> u64 {
        self.state = (self.state * 1103515245 + 12345) % 2147483648; // 2^31
        self.state
    }
}

fn env_str(name: &str) -> Result<String, String> {
    std::env::var(name).map_err(|_| format!("missing environment variable {}", name))
}

fn env_u64(name: &str) -> Result<u64, String> {
    let raw = env_str(name)?;
    raw.trim()
        .parse::<u64>()
        .map_err(|e| format!("invalid integer in {}={:?}: {}", name, raw, e))
}

/// Like env_u64 but falls back to `default` when the variable is unset or empty.
fn env_u64_or(name: &str, default: u64) -> Result<u64, String> {
    match std::env::var(name) {
        Ok(raw) if !raw.trim().is_empty() => raw
            .trim()
            .parse::<u64>()
            .map_err(|e| format!("invalid integer in {}={:?}: {}", name, raw, e)),
        _ => Ok(default),
    }
}

fn client_host() -> String {
    if let Ok(h) = std::env::var("HOSTNAME") {
        let h = h.trim().to_string();
        if !h.is_empty() {
            return h;
        }
    }
    if let Ok(h) = std::fs::read_to_string("/proc/sys/kernel/hostname") {
        let h = h.trim().to_string();
        if !h.is_empty() {
            return h;
        }
    }
    "unknown".to_string()
}

/// Shared read-only benchmark configuration.
struct Cfg {
    host: String,
    port: u16,
    user: String,
    password: String,
    db: String,
    workload: u8,
    threads: u64,
    seed: u64,
    int_rows: u64,
    int_batch: u64,
    blob_count: u64,
    blob_size: u64,
}

fn connect(cfg: &Cfg) -> Result<Conn, mysql::Error> {
    // One TCP connection per worker; autocommit ON (server default).
    // prefer_socket(false): never switch to a unix socket for localhost.
    // The `minimal` feature compiles out TLS entirely, so the connection is
    // plaintext TCP (SPEC: TLS disabled) — no ssl_opts needed.
    let opts = OptsBuilder::new()
        .ip_or_hostname(Some(cfg.host.as_str()))
        .tcp_port(cfg.port)
        .user(Some(cfg.user.as_str()))
        .pass(Some(cfg.password.as_str()))
        .db_name(Some(cfg.db.as_str()))
        .prefer_socket(false);
    Conn::new(opts)
}

type WorkerErr = Box<dyn std::error::Error>;

/// Worker body. Connects, prepares, waits at the start barrier, then runs the
/// timed workload. `slot` is this worker's progress counter (completed ops).
/// Returns (per-operation latencies in µs, total completed operations).
fn worker_inner(
    cfg: &Cfg,
    barrier: &Barrier,
    idx: u64,
    slot: &AtomicU64,
) -> Result<(Vec<u64>, u64), WorkerErr> {
    let mut conn = connect(cfg)?;
    let mut rng = Lcg::new(cfg.seed + idx);

    match cfg.workload {
        1 => {
            // ---- WL1: integer insert ----------------------------------------
            // One prepared multi-row INSERT of BENCH_INT_BATCH rows, executed
            // BENCH_INT_ROWS / BENCH_INT_BATCH times. Each bound value is a
            // fresh next() cast to a signed 32-bit int. One latency sample per
            // executed INSERT statement (the exec round trip). Autocommit ON:
            // each INSERT is its own commit (no wrapping transaction) — SPEC
            // requires this identical commit strategy in every language.
            let batch = cfg.int_batch as usize;
            let num_batches = cfg.int_rows / cfg.int_batch;
            let placeholders = vec!["(?)"; batch].join(",");
            let sql = format!("INSERT INTO bench_int (val) VALUES {}", placeholders);
            let stmt = conn.prep(sql.as_str())?;

            let mut latencies = Vec::with_capacity(num_batches as usize);
            let mut done: u64 = 0;

            barrier.wait();

            for _ in 0..num_batches {
                // Draw the batch values before timing: the sample measures the
                // INSERT statement, not client-side value generation.
                let mut vals: Vec<Value> = Vec::with_capacity(batch);
                for _ in 0..batch {
                    vals.push(Value::from(rng.next() as i32));
                }
                let t0 = Instant::now();
                conn.exec_drop(&stmt, Params::Positional(vals))?;
                latencies.push(t0.elapsed().as_micros() as u64);

                done += cfg.int_batch;
                slot.store(done, Ordering::Relaxed);
            }

            Ok((latencies, done))
        }
        2 => {
            // ---- WL2: integer fetch + sum -----------------------------------
            // SELECT val FROM bench_int LIMIT BENCH_INT_ROWS, fetch every row,
            // sum val in a 64-bit accumulator fed to a black-box sink. One
            // latency sample per worker for the whole scan.
            let sql = format!("SELECT val FROM bench_int LIMIT {}", cfg.int_rows);
            let stmt = conn.prep(sql.as_str())?;

            barrier.wait();

            let t0 = Instant::now();
            let mut sum: i64 = 0;
            let mut rows_read: u64 = 0;
            let result = conn.exec_iter(&stmt, ())?;
            for row in result {
                let row = row?;
                let v: i64 = row.get(0).ok_or("WL2: row missing column val")?;
                sum = sum.wrapping_add(v);
                rows_read += 1;
                // Publish progress periodically (cheap relaxed store); the full
                // scan is a single latency sample but the slot should advance.
                if rows_read % 4096 == 0 {
                    slot.store(rows_read, Ordering::Relaxed);
                }
            }
            let sample = t0.elapsed().as_micros() as u64;
            slot.store(rows_read, Ordering::Relaxed);
            std::hint::black_box(sum);

            Ok((vec![sample], rows_read))
        }
        3 => {
            // ---- WL3: blob insert + MD5 -------------------------------------
            // SPEC: content generation must NOT be a per-byte interpreted loop.
            // Fill ONE BENCH_BLOB_SIZE buffer per worker ONCE with a fast bulk
            // LCG fill (8 bytes per step via next().to_le_bytes()), then before
            // each of BENCH_BLOB_COUNT blobs overwrite the first 8 bytes with
            // the blob id so every blob's content (and MD5) differs. MD5 hashes
            // the whole buffer each time; INSERT (id, data), id = idx*COUNT + k,
            // autocommit ON (each INSERT its own commit). One latency sample per
            // blob insert (the exec round trip; the id-stamp, MD5, and the owned
            // copy for the driver are client-side, excluded from the sample but
            // inside the timed window).
            let stmt = conn.prep("INSERT INTO bench_blob (id, data) VALUES (?, ?)")?;
            let size = cfg.blob_size as usize;

            // One-time bulk fill (setup, before the start barrier).
            let mut buf: Vec<u8> = vec![0u8; size];
            let mut off = 0;
            while off < size {
                let bytes = rng.next().to_le_bytes(); // 8 bytes per LCG step
                let take = (size - off).min(8);
                buf[off..off + take].copy_from_slice(&bytes[..take]);
                off += take;
            }
            let stamp = size.min(8); // guard tiny BENCH_BLOB_SIZE (< 8)

            let mut latencies = Vec::with_capacity(cfg.blob_count as usize);
            let mut done: u64 = 0;

            barrier.wait();

            for k in 0..cfg.blob_count {
                let id = (idx * cfg.blob_count + k) as i64;
                // Stamp the id into the first bytes: distinct content & MD5.
                buf[..stamp].copy_from_slice(&id.to_le_bytes()[..stamp]);

                let mut hasher = Md5::new();
                hasher.update(&buf);
                std::hint::black_box(hasher.finalize());

                // Owned copy for the driver, built before timing (the buffer is
                // reused across blobs, so it cannot be moved into the INSERT).
                let data = buf.clone();
                let t0 = Instant::now();
                conn.exec_drop(&stmt, (id, data))?;
                latencies.push(t0.elapsed().as_micros() as u64);

                done += 1;
                slot.store(done, Ordering::Relaxed);
            }

            Ok((latencies, done))
        }
        4 => {
            // ---- WL4: blob read + MD5 ---------------------------------------
            // Read this worker's own BENCH_BLOB_COUNT blobs by id range
            // (SELECT data WHERE id = ?, ids idx*BLOB_COUNT + k) and MD5 each
            // blob's content in app code (digest to a sink). One latency sample
            // per blob read (the SELECT round trip; MD5 excluded from the
            // sample, included in the timed window).
            let stmt = conn.prep("SELECT data FROM bench_blob WHERE id = ?")?;

            let mut latencies = Vec::with_capacity(cfg.blob_count as usize);
            let mut done: u64 = 0;

            barrier.wait();

            for k in 0..cfg.blob_count {
                let id = (idx * cfg.blob_count + k) as i64;
                let t0 = Instant::now();
                // 1-tuple FromRow is always implemented; a bare scalar FromRow
                // is not guaranteed in mysql_common.
                let (data,): (Vec<u8>,) = conn
                    .exec_first(&stmt, (id,))?
                    .ok_or("WL4: blob not found (did workload 3 run at this thread count?)")?;
                latencies.push(t0.elapsed().as_micros() as u64);

                let mut hasher = Md5::new();
                hasher.update(&data);
                std::hint::black_box(hasher.finalize());

                done += 1;
                slot.store(done, Ordering::Relaxed);
            }

            Ok((latencies, done))
        }
        w => Err(format!("invalid BENCH_WORKLOAD {} (must be 1..4)", w).into()),
    }
}

fn worker(cfg: &Cfg, barrier: &Barrier, idx: u64, slot: &AtomicU64) -> (Vec<u64>, u64) {
    match worker_inner(cfg, barrier, idx, slot) {
        Ok(res) => res,
        Err(e) => {
            eprintln!("error: worker {}: {}", idx, e);
            std::process::exit(1);
        }
    }
}

/// Atomically overwrite the progress file with one partial JSON line
/// (write FILE.tmp, then rename over FILE). Best-effort: write failures are
/// warned about but do not abort an otherwise-valid run.
fn write_progress(
    path: &str,
    workload: u8,
    name: &str,
    unit: &str,
    threads: u64,
    ops: u64,
    elapsed_s: f64,
) {
    let throughput = if elapsed_s > 0.0 {
        ops as f64 / elapsed_s
    } else {
        0.0
    };
    let line = format!(
        "{{\"workload\":{},\"workload_name\":\"{}\",\"unit\":\"{}\",\"threads\":{},\
\"ops\":{},\"elapsed_s\":{:.3},\"throughput\":{:.1},\"partial\":true}}\n",
        workload, name, unit, threads, ops, elapsed_s, throughput
    );
    let tmp = format!("{}.tmp", path);
    if let Err(e) = fs::write(&tmp, line.as_bytes()) {
        eprintln!("warning: progress write {}: {}", tmp, e);
        return;
    }
    if let Err(e) = fs::rename(&tmp, path) {
        eprintln!("warning: progress rename {} -> {}: {}", tmp, path, e);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    // Connection config.
    let host = env_str("MYSQL_HOST")?;
    let port: u16 = env_str("MYSQL_PORT")?
        .trim()
        .parse()
        .map_err(|e| format!("invalid MYSQL_PORT: {}", e))?;
    let user = env_str("MYSQL_USER")?;
    let password = env_str("MYSQL_PASSWORD")?;
    let db = env_str("MYSQL_DB")?;

    // Workload config.
    let cfg = Cfg {
        host,
        port,
        user,
        password,
        db,
        workload: env_u64("BENCH_WORKLOAD")? as u8,
        threads: env_u64("BENCH_THREADS")?,
        seed: env_u64("BENCH_SEED")?,
        int_rows: env_u64_or("BENCH_INT_ROWS", 1_000_000)?,
        int_batch: env_u64_or("BENCH_INT_BATCH", 10)?,
        blob_count: env_u64_or("BENCH_BLOB_COUNT", 100)?,
        blob_size: env_u64_or("BENCH_BLOB_SIZE", 1_048_576)?,
    };

    if cfg.threads == 0 {
        return Err("BENCH_THREADS must be > 0".into());
    }
    let (name, unit, is_blob) = match cfg.workload {
        1 => ("int-insert", "rows/s", false),
        2 => ("int-scan", "rows/s", false),
        3 => ("blob-insert", "blobs/s", true),
        4 => ("blob-read", "blobs/s", true),
        w => return Err(format!("invalid BENCH_WORKLOAD {} (must be 1..4)", w).into()),
    };
    // Per-workload validation so we never produce zero latency samples.
    match cfg.workload {
        1 => {
            if cfg.int_batch == 0 {
                return Err("BENCH_INT_BATCH must be > 0".into());
            }
            if cfg.int_rows / cfg.int_batch == 0 {
                return Err("BENCH_INT_ROWS must be >= BENCH_INT_BATCH".into());
            }
        }
        2 => {
            if cfg.int_rows == 0 {
                return Err("BENCH_INT_ROWS must be > 0".into());
            }
        }
        3 | 4 => {
            if cfg.blob_count == 0 {
                return Err("BENCH_BLOB_COUNT must be > 0".into());
            }
            if cfg.blob_size == 0 {
                return Err("BENCH_BLOB_SIZE must be > 0".into());
            }
        }
        _ => unreachable!(),
    }

    // Progress checkpointing (optional).
    let progress_path: Option<String> = match std::env::var("BENCH_PROGRESS_FILE") {
        Ok(p) if !p.trim().is_empty() => Some(p),
        _ => None,
    };
    let progress_sec = env_u64_or("BENCH_PROGRESS_SEC", 5)?.max(1);

    // One progress slot per worker; each worker writes only its own slot.
    let counters: Vec<AtomicU64> = (0..cfg.threads).map(|_| AtomicU64::new(0)).collect();

    // Start barrier: every worker (after connect + prepare) plus main. The
    // monitor does NOT wait on the barrier (it polls `started`).
    let barrier = Barrier::new(cfg.threads as usize + 1);
    let started = AtomicBool::new(false);
    let finished = AtomicBool::new(false);

    let (mut latencies, total_ops, elapsed_s) = thread::scope(|s| {
        let cfg_ref = &cfg;
        let barrier_ref = &barrier;
        let counters_ref = &counters;

        // Worker threads.
        let handles: Vec<_> = (0..cfg.threads)
            .map(|i| {
                s.spawn(move || worker(cfg_ref, barrier_ref, i, &counters_ref[i as usize]))
            })
            .collect();

        // Monitor thread: samples the per-worker slots on a timer and writes
        // the partial progress file. No-op when progress is disabled.
        let started_ref = &started;
        let finished_ref = &finished;
        let progress_ref = &progress_path;
        s.spawn(move || {
            let path = match progress_ref {
                Some(p) => p.as_str(),
                None => return,
            };
            let tick = Duration::from_millis(100);
            while !started_ref.load(Ordering::Acquire) {
                if finished_ref.load(Ordering::Acquire) {
                    return;
                }
                thread::sleep(tick);
            }
            // Use the monitor's own start instant for the partial elapsed; the
            // sub-millisecond skew from main's t_start is irrelevant here
            // ("slightly-stale counts are fine").
            let mon_start = Instant::now();
            let interval = Duration::from_secs(progress_sec);
            let mut next = mon_start + interval;
            while !finished_ref.load(Ordering::Acquire) {
                let now = Instant::now();
                if now >= next {
                    let ops: u64 = counters_ref.iter().map(|c| c.load(Ordering::Relaxed)).sum();
                    let elapsed = (now - mon_start).as_secs_f64();
                    write_progress(path, cfg_ref.workload, name, unit, cfg_ref.threads, ops, elapsed);
                    next += interval;
                }
                thread::sleep(tick);
            }
        });

        // Release the barrier; sample t_start; signal the monitor.
        barrier_ref.wait();
        let t_start = Instant::now();
        started.store(true, Ordering::Release);

        let mut results = Vec::with_capacity(handles.len());
        for h in handles {
            match h.join() {
                Ok(res) => results.push(res),
                Err(_) => {
                    eprintln!("error: worker thread panicked");
                    std::process::exit(1);
                }
            }
        }
        let elapsed_s = t_start.elapsed().as_secs_f64();
        // Stop the monitor so the scope can join it.
        finished.store(true, Ordering::Release);

        let total: usize = results.iter().map(|(lats, _)| lats.len()).sum();
        let mut merged: Vec<u64> = Vec::with_capacity(total);
        let mut total_ops: u64 = 0;
        for (lats, ops) in results {
            merged.extend_from_slice(&lats);
            total_ops += ops;
        }
        (merged, total_ops, elapsed_s)
    });

    latencies.sort_unstable();
    let n = latencies.len() as u64;
    // p(x) = arr[ceil(x/100 * n) - 1], 0-based.
    let pct = |x: u64| latencies[(((x * n + 99) / 100) - 1) as usize];
    let p50 = pct(50);
    let p95 = pct(95);
    let p99 = pct(99);
    let max_us = latencies[latencies.len() - 1];

    let throughput = total_ops as f64 / elapsed_s;
    let mb_per_s = if is_blob {
        format!("{:.1}", total_ops as f64 * cfg.blob_size as f64 / 1e6 / elapsed_s)
    } else {
        "0".to_string()
    };

    println!(
        "{{\"lang\":\"rust\",\"workload\":{},\"workload_name\":\"{}\",\"unit\":\"{}\",\
\"driver\":\"mysql crate\",\"driver_version\":\"{}\",\"runtime_version\":\"{}\",\
\"prepared\":true,\"client_host\":\"{}\",\"db_host\":\"{}\",\"threads\":{},\"ops\":{},\
\"errors\":0,\"elapsed_s\":{:.3},\"throughput\":{:.1},\"mb_per_s\":{},\
\"p50_us\":{},\"p95_us\":{},\"p99_us\":{},\"max_us\":{},\"client_cpu_s\":0,\"server_cpu_s\":0}}",
        cfg.workload,
        name,
        unit,
        DRIVER_VERSION,
        env!("RUSTC_VERSION"),
        client_host(),
        cfg.host,
        cfg.threads,
        total_ops,
        elapsed_s,
        throughput,
        mb_per_s,
        p50,
        p95,
        p99,
        max_us,
    );

    // Success: the stdout line supersedes the partial file — delete it.
    if let Some(path) = &progress_path {
        let _ = fs::remove_file(path);
        let _ = fs::remove_file(format!("{}.tmp", path));
    }

    Ok(())
}

fn main() {
    if let Err(e) = run() {
        eprintln!("error: {}", e);
        std::process::exit(1);
    }
}
