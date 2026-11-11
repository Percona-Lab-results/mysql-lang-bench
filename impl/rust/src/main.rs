// MySQL language benchmark — Rust implementation (SPEC.md v2).
// Multithreaded sysbench-oltp_read_write-style transactional workload:
// each worker thread owns one connection, its own server-side prepared
// statements and its own PRNG stream. Timing is barrier-based.

use std::sync::Barrier;
use std::time::{Duration, Instant};

use mysql::prelude::Queryable;
use mysql::{Conn, OptsBuilder, Row, Statement};

/// Must match the exact version pinned in Cargo.toml.
const DRIVER_VERSION: &str = "25.0.1";

const SQL_POINT_SELECT: &str = "SELECT c FROM sbtest WHERE id = ?";
const SQL_SIMPLE_RANGE: &str = "SELECT c FROM sbtest WHERE id BETWEEN ? AND ?";
const SQL_SUM_RANGE: &str = "SELECT SUM(k) FROM sbtest WHERE id BETWEEN ? AND ?";
const SQL_ORDER_RANGE: &str = "SELECT c FROM sbtest WHERE id BETWEEN ? AND ? ORDER BY c";
const SQL_DISTINCT_RANGE: &str =
    "SELECT DISTINCT c FROM sbtest WHERE id BETWEEN ? AND ? ORDER BY c";
const SQL_INDEX_UPDATE: &str = "UPDATE sbtest SET k = k + 1 WHERE id = ?";
const SQL_NON_INDEX_UPDATE: &str = "UPDATE sbtest SET c = ? WHERE id = ?";
const SQL_DELETE: &str = "DELETE FROM sbtest WHERE id = ?";
const SQL_INSERT: &str = "INSERT INTO sbtest (id, k, c, pad) VALUES (?, ?, ?, ?)";

/// 31-bit LCG per SPEC.md. State and products fit in u64 (state < 2^31,
/// 2^31 * 1103515245 < 2^62), so plain u64 arithmetic is exact.
/// Validation (seed 42): rand(100) -> 27, 64, 53, 6, 35.
struct Lcg {
    state: u64,
}

impl Lcg {
    fn new(seed: u64) -> Self {
        Lcg { state: seed }
    }

    fn next(&mut self) -> u64 {
        self.state = (self.state * 1103515245 + 12345) % 2147483648; // 2^31
        self.state
    }

    /// Uniform 0..n-1.
    fn rand(&mut self, n: u64) -> u64 {
        self.next() % n
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

/// Like env_u64 but falls back to `default` when the variable is unset.
fn env_u64_or(name: &str, default: u64) -> Result<u64, String> {
    match std::env::var(name) {
        Ok(raw) => raw
            .trim()
            .parse::<u64>()
            .map_err(|e| format!("invalid integer in {}={:?}: {}", name, raw, e)),
        Err(_) => Ok(default),
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
    table_rows: u64,
    threads: u64,
    warmup_txns: u64,
    txns_per_thread: u64,
    duration_s: u64, // 0 = events mode, > 0 = duration mode
    range_size: u64,
    seed: u64,
    c_val: String,   // "abcdefghij" x 12 (120 chars)
    pad_val: String, // "0123456789" x 6 (60 chars)
}

/// The 18 server-side prepared statements of one transaction, one set per
/// worker. The mysql crate caches prepared statements by SQL text, so the 10
/// identical point-select slots share one server-side statement handle —
/// exactly equivalent to preparing it once and executing it 10 times.
struct Stmts {
    point_selects: Vec<Statement>, // 10
    simple_range: Statement,
    sum_range: Statement,
    order_range: Statement,
    distinct_range: Statement,
    index_update: Statement,
    non_index_update: Statement,
    delete: Statement,
    insert: Statement,
}

fn prepare_all(conn: &mut Conn) -> Result<Stmts, mysql::Error> {
    let mut point_selects = Vec::with_capacity(10);
    for _ in 0..10 {
        point_selects.push(conn.prep(SQL_POINT_SELECT)?);
    }
    Ok(Stmts {
        point_selects,
        simple_range: conn.prep(SQL_SIMPLE_RANGE)?,
        sum_range: conn.prep(SQL_SUM_RANGE)?,
        order_range: conn.prep(SQL_ORDER_RANGE)?,
        distinct_range: conn.prep(SQL_DISTINCT_RANGE)?,
        index_update: conn.prep(SQL_INDEX_UPDATE)?,
        non_index_update: conn.prep(SQL_NON_INDEX_UPDATE)?,
        delete: conn.prep(SQL_DELETE)?,
        insert: conn.prep(SQL_INSERT)?,
    })
}

/// Consume a `SELECT c ...` result fully (all rows), folding into the sink.
fn consume_c_rows(rows: Vec<Row>, sink: &mut usize) {
    for row in &rows {
        if let Some(c) = row.get::<String, _>(0) {
            *sink = sink.wrapping_add(c.len());
        }
    }
}

/// One attempt at the oltp_read_write transaction. Values are drawn
/// immediately before executing each statement (draw order = execution order).
fn try_txn(
    conn: &mut Conn,
    st: &Stmts,
    rng: &mut Lcg,
    cfg: &Cfg,
    sink: &mut usize,
) -> Result<(), mysql::Error> {
    let n = cfg.table_rows;
    let r = cfg.range_size;

    conn.query_drop("BEGIN")?;

    // 10 point selects.
    for stmt in &st.point_selects {
        let id = 1 + rng.rand(n);
        let rows: Vec<Row> = conn.exec(stmt, (id,))?;
        consume_c_rows(rows, sink);
    }

    // Simple range.
    let lo = 1 + rng.rand(n);
    let rows: Vec<Row> = conn.exec(&st.simple_range, (lo, lo + r - 1))?;
    consume_c_rows(rows, sink);

    // Sum range. SUM(k) is DECIMAL (NULL when the range matches no rows);
    // with minimal features decode it as Option<String>.
    let lo = 1 + rng.rand(n);
    let rows: Vec<Row> = conn.exec(&st.sum_range, (lo, lo + r - 1))?;
    for row in &rows {
        if let Some(Some(s)) = row.get::<Option<String>, _>(0) {
            *sink = sink.wrapping_add(s.len());
        }
    }

    // Order range.
    let lo = 1 + rng.rand(n);
    let rows: Vec<Row> = conn.exec(&st.order_range, (lo, lo + r - 1))?;
    consume_c_rows(rows, sink);

    // Distinct range.
    let lo = 1 + rng.rand(n);
    let rows: Vec<Row> = conn.exec(&st.distinct_range, (lo, lo + r - 1))?;
    consume_c_rows(rows, sink);

    // Index update.
    let id = 1 + rng.rand(n);
    conn.exec_drop(&st.index_update, (id,))?;

    // Non-index update.
    let id = 1 + rng.rand(n);
    conn.exec_drop(&st.non_index_update, (cfg.c_val.as_str(), id))?;

    // Delete + insert (same id).
    let id = 1 + rng.rand(n);
    let k = rng.rand(n);
    conn.exec_drop(&st.delete, (id,))?;
    conn.exec_drop(
        &st.insert,
        (id, k, cfg.c_val.as_str(), cfg.pad_val.as_str()),
    )?;

    conn.query_drop("COMMIT")?;
    Ok(())
}

/// Deadlock (1213), lock wait timeout (1205), duplicate key (1062).
fn is_retryable(e: &mysql::Error) -> bool {
    matches!(e, mysql::Error::MySqlError(me)
        if me.code == 1213 || me.code == 1205 || me.code == 1062)
}

/// Run one transaction to successful COMMIT, retrying on concurrency errors
/// with freshly drawn values (the PRNG stream continues). Non-retryable
/// errors bubble up.
fn run_txn(
    conn: &mut Conn,
    st: &Stmts,
    rng: &mut Lcg,
    cfg: &Cfg,
    sink: &mut usize,
    retries: &mut u64,
) -> Result<(), mysql::Error> {
    loop {
        match try_txn(conn, st, rng, cfg, sink) {
            Ok(()) => return Ok(()),
            Err(e) if is_retryable(&e) => {
                let _ = conn.query_drop("ROLLBACK"); // ignore rollback errors
                *retries += 1;
            }
            Err(e) => return Err(e),
        }
    }
}

/// Worker body: connect, prepare, warm up, wait at the start barrier, run the
/// timed transactions. Returns (latencies_us, retries).
fn worker_inner(
    cfg: &Cfg,
    barrier: &Barrier,
    idx: u64,
) -> Result<(Vec<u64>, u64), mysql::Error> {
    // One TCP connection per worker, autocommit ON (server default).
    // prefer_socket(false): never switch to a unix socket for localhost.
    let opts = OptsBuilder::new()
        .ip_or_hostname(Some(cfg.host.as_str()))
        .tcp_port(cfg.port)
        .user(Some(cfg.user.as_str()))
        .pass(Some(cfg.password.as_str()))
        .db_name(Some(cfg.db.as_str()))
        .prefer_socket(false);
    let mut conn = Conn::new(opts)?;
    let st = prepare_all(&mut conn)?;

    let mut rng = Lcg::new(cfg.seed + idx);
    let mut sink: usize = 0; // consumes fetched SELECT data

    // Warmup (untimed; its retries are not reported).
    let mut warmup_retries = 0u64;
    for _ in 0..cfg.warmup_txns {
        run_txn(&mut conn, &st, &mut rng, cfg, &mut sink, &mut warmup_retries)?;
    }

    barrier.wait();

    // Timed phase. Latency window: just before the first BEGIN of the txn to
    // after its successful COMMIT, retries included. Integer microseconds.
    let mut latencies: Vec<u64>;
    let mut retries = 0u64;
    if cfg.duration_s > 0 {
        // Duration mode: run until the per-worker deadline, checked before
        // starting each new transaction; a transaction in flight at the
        // deadline (its retries included) runs to successful commit.
        latencies = Vec::with_capacity(4096);
        let start = Instant::now();
        let duration = Duration::from_secs(cfg.duration_s);
        while start.elapsed() < duration {
            let t0 = Instant::now();
            run_txn(&mut conn, &st, &mut rng, cfg, &mut sink, &mut retries)?;
            latencies.push(t0.elapsed().as_micros() as u64);
        }
    } else {
        // Events mode: exactly BENCH_TXNS_PER_THREAD timed transactions.
        latencies = Vec::with_capacity(cfg.txns_per_thread as usize);
        for _ in 0..cfg.txns_per_thread {
            let t0 = Instant::now();
            run_txn(&mut conn, &st, &mut rng, cfg, &mut sink, &mut retries)?;
            latencies.push(t0.elapsed().as_micros() as u64);
        }
    }

    std::hint::black_box(sink);
    drop(conn);
    Ok((latencies, retries))
}

fn worker(cfg: &Cfg, barrier: &Barrier, idx: u64) -> (Vec<u64>, u64) {
    match worker_inner(cfg, barrier, idx) {
        Ok(res) => res,
        Err(e) => {
            eprintln!("error: worker {}: {}", idx, e);
            std::process::exit(1);
        }
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
        table_rows: env_u64("BENCH_TABLE_ROWS")?,
        threads: env_u64("BENCH_THREADS")?,
        warmup_txns: env_u64("BENCH_WARMUP_TXNS")?,
        txns_per_thread: env_u64("BENCH_TXNS_PER_THREAD")?,
        duration_s: env_u64_or("BENCH_DURATION_S", 0)?,
        range_size: env_u64_or("BENCH_RANGE_SIZE", 100)?,
        seed: env_u64("BENCH_SEED")?,
        c_val: "abcdefghij".repeat(12),   // 120 chars
        pad_val: "0123456789".repeat(6), // 60 chars
    };

    if cfg.table_rows == 0 || cfg.threads == 0 || cfg.range_size == 0 {
        return Err("BENCH_TABLE_ROWS, BENCH_THREADS and BENCH_RANGE_SIZE must be > 0".into());
    }
    // BENCH_TXNS_PER_THREAD is ignored in duration mode (BENCH_DURATION_S > 0).
    if cfg.duration_s == 0 && cfg.txns_per_thread == 0 {
        return Err("BENCH_TXNS_PER_THREAD must be > 0 in events mode".into());
    }

    // Start barrier: all workers (after connect + prepare + warmup) plus the
    // main thread. Main's wait() marks t_start.
    let barrier = Barrier::new(cfg.threads as usize + 1);

    let (mut latencies, total_retries, elapsed_s) = std::thread::scope(|s| {
        let handles: Vec<_> = (0..cfg.threads)
            .map(|i| {
                let cfg = &cfg;
                let barrier = &barrier;
                s.spawn(move || worker(cfg, barrier, i))
            })
            .collect();

        barrier.wait();
        let t_start = Instant::now();

        let mut results = Vec::with_capacity(handles.len());
        for h in handles {
            match h.join() {
                Ok(res) => results.push(res),
                Err(_) => {
                    // Worker panic message already went to stderr.
                    eprintln!("error: worker thread panicked");
                    std::process::exit(1);
                }
            }
        }
        let elapsed_s = t_start.elapsed().as_secs_f64();

        let total: usize = results.iter().map(|(lats, _)| lats.len()).sum();
        let mut merged: Vec<u64> = Vec::with_capacity(total);
        let mut total_retries = 0u64;
        for (lats, retries) in results {
            merged.extend_from_slice(&lats);
            total_retries += retries;
        }
        (merged, total_retries, elapsed_s)
    });

    latencies.sort_unstable();
    let n = latencies.len() as u64;
    // p(x) = arr[ceil(x/100 * n) - 1], 0-based.
    let pct = |x: u64| latencies[(((x * n + 99) / 100) - 1) as usize];
    let p50 = pct(50);
    let p95 = pct(95);
    let p99 = pct(99);
    let max_us = latencies[latencies.len() - 1];

    // Duration mode: txns = total committed timed transactions across workers
    // (one latency sample per committed timed transaction). Events mode:
    // exactly BENCH_THREADS * BENCH_TXNS_PER_THREAD, as before.
    let txns = if cfg.duration_s > 0 {
        n
    } else {
        cfg.threads * cfg.txns_per_thread
    };
    let tps = txns as f64 / elapsed_s;
    let qps = tps * 20.0; // 18 statements + BEGIN + COMMIT (sysbench counting)

    println!(
        "{{\"lang\":\"rust\",\"driver\":\"mysql crate\",\"driver_version\":\"{}\",\
\"runtime_version\":\"{}\",\"prepared\":true,\"client_host\":\"{}\",\"db_host\":\"{}\",\
\"threads\":{},\"txns\":{},\"retries\":{},\"errors\":0,\"elapsed_s\":{:.3},\
\"tps\":{:.1},\"qps\":{:.1},\"p50_us\":{},\"p95_us\":{},\"p99_us\":{},\"max_us\":{}}}",
        DRIVER_VERSION,
        env!("RUSTC_VERSION"),
        client_host(),
        cfg.host,
        cfg.threads,
        txns,
        total_retries,
        elapsed_s,
        tps,
        qps,
        p50,
        p95,
        p99,
        max_us
    );
    Ok(())
}

fn main() {
    if let Err(e) = run() {
        eprintln!("error: {}", e);
        std::process::exit(1);
    }
}
