// mysql-lang-bench — C++ implementation (v3, four micro-workloads).
//
// libmysqlclient C API with server-side prepared statements, std::thread
// workers (one dedicated MYSQL* connection each). One workload per run,
// selected by BENCH_WORKLOAD (1..4); one JSON line on stdout. See SPEC.md.
//
//   WL1 int-insert : multi-row prepared INSERT into bench_int (batched)
//   WL2 int-read   : SELECT val ... LIMIT N, sum in app code -> black box
//   WL3 blob-insert: generate BENCH_BLOB_SIZE random bytes, MD5 in app,
//                    INSERT (id, data) into bench_blob
//   WL4 blob-read  : SELECT data WHERE id = ?, MD5 in app code
//
// Build: c++ -std=c++17 -O2 -pthread bench.cpp -o bench \
//        $(mysql_config --cflags --libs) -lcrypto

#include <mysql.h>

// OpenSSL >= 3.0 deprecates the low-level MD5 API; SPEC pins <openssl/md5.h>,
// so suppress the deprecation diagnostics rather than switch to EVP.
#define OPENSSL_SUPPRESS_DEPRECATED
#include <openssl/md5.h>

#include <pthread.h> // pthread_barrier_t (C++17 has no std::barrier)
#include <unistd.h>  // gethostname

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cinttypes>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <type_traits>
#include <vector>

// MYSQL_BIND::is_null / ::error are `my_bool*` on older clients and `bool*`
// on MySQL >= 8.0.1; derive the element type portably from the header.
using bind_bool = std::remove_pointer_t<decltype(MYSQL_BIND::is_null)>;

// ---- error helpers ----------------------------------------------------------
// die* may be called from worker threads: std::exit terminates the whole
// process, which is what SPEC.md wants for non-retryable errors (errors == 0,
// non-zero exit, no retries).

[[noreturn]] static void die(const std::string &msg) {
    std::fprintf(stderr, "error: %s\n", msg.c_str());
    std::exit(1);
}

[[noreturn]] static void die_conn(MYSQL *conn, const char *what) {
    std::fprintf(stderr, "error: %s: (%u) %s\n", what, mysql_errno(conn),
                 mysql_error(conn));
    std::exit(1);
}

[[noreturn]] static void die_stmt(MYSQL_STMT *stmt, const char *what) {
    std::fprintf(stderr, "error: %s: (%u) %s\n", what, mysql_stmt_errno(stmt),
                 mysql_stmt_error(stmt));
    std::exit(1);
}

// ---- env parsing ------------------------------------------------------------

static const char *env_str(const char *name) {
    const char *v = std::getenv(name);
    if (!v || !*v)
        die(std::string("missing environment variable ") + name);
    return v;
}

static int64_t parse_int(const char *name, const char *v) {
    char *end = nullptr;
    errno = 0;
    long long n = std::strtoll(v, &end, 10);
    if (errno != 0 || end == v || *end != '\0')
        die(std::string(name) + " is not an integer: " + v);
    return static_cast<int64_t>(n);
}

static int64_t env_int(const char *name) { return parse_int(name, env_str(name)); }

static int64_t env_int_or(const char *name, int64_t def) {
    const char *v = std::getenv(name);
    if (!v || !*v)
        return def;
    return parse_int(name, v);
}

static std::string env_str_or(const char *name, const char *def) {
    const char *v = std::getenv(name);
    return (v && *v) ? std::string(v) : std::string(def);
}

// ---- configuration (written once in main, before any thread starts) ---------

struct Config {
    const char *host = nullptr;
    const char *user = nullptr;
    const char *password = nullptr;
    const char *db = nullptr;
    unsigned int port = 0;

    int64_t workload = 0;
    int64_t threads = 0;
    int64_t seed = 0;
    int64_t int_rows = 0;
    int64_t int_batch = 0;
    int64_t blob_count = 0;
    int64_t blob_size = 0;

    std::string progress_file;
    int64_t progress_sec = 5;
};

static Config g_cfg;
static pthread_barrier_t g_start_barrier;

// Black-box sink: keeps app-computed sums / MD5 digests from being optimized
// away. Relaxed atomic RMW on a file-scope object is an observable side effect
// the compiler cannot elide.
static std::atomic<uint64_t> g_sink{0};
static inline void black_box(uint64_t v) {
    g_sink.fetch_xor(v, std::memory_order_relaxed);
}

// ---- PRNG (31-bit LCG, identical in every language) -------------------------
// state = (state * 1103515245 + 12345) mod 2^31. Worker i seeds BENCH_SEED+i.
// Validation (seed 42, worker 0): first next() = 1250496027.

static uint64_t seed31(int64_t s) {
    int64_t m = ((s % 2147483648LL) + 2147483648LL) % 2147483648LL;
    return static_cast<uint64_t>(m);
}

// ---- per-worker state -------------------------------------------------------
// Workers share no mutable state except `progress` (written only by its owner,
// read by the monitor via a relaxed atomic). `lat` is read by main after join.

struct Worker {
    int index = 0;
    MYSQL *conn = nullptr;
    uint64_t lcg = 0;                 // 31-bit LCG state
    std::vector<uint64_t> lat;        // per-op latency, integer microseconds
    std::atomic<long> progress{0};    // completed ops (rows WL1/2, blobs WL3/4)

    inline uint64_t next() {
        lcg = (lcg * 1103515245ULL + 12345ULL) & 0x7FFFFFFFULL;
        return lcg;
    }
};

// ---- clock ------------------------------------------------------------------

using clock_t_ = std::chrono::steady_clock;

static inline uint64_t us_since(clock_t_::time_point t0) {
    return static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::microseconds>(clock_t_::now() -
                                                              t0)
            .count());
}

// ---- connect ----------------------------------------------------------------

static void worker_connect(Worker &w) {
    w.conn = mysql_init(nullptr);
    if (!w.conn)
        die("mysql_init failed (out of memory?)");

    unsigned int proto = MYSQL_PROTOCOL_TCP; // never a unix socket
    mysql_options(w.conn, MYSQL_OPT_PROTOCOL, &proto);
    // Plaintext everywhere: libmysqlclient defaults to SSL_MODE_PREFERRED,
    // which would put this driver on TLS while others run plaintext.
    unsigned int ssl_mode = SSL_MODE_DISABLED;
    mysql_options(w.conn, MYSQL_OPT_SSL_MODE, &ssl_mode);
    // caching_sha2_password full auth over plaintext needs the server RSA key.
    bool get_server_pk = true;
    mysql_options(w.conn, MYSQL_OPT_GET_SERVER_PUBLIC_KEY, &get_server_pk);

    if (!mysql_real_connect(w.conn, g_cfg.host, g_cfg.user, g_cfg.password,
                            g_cfg.db, g_cfg.port, nullptr, 0))
        die_conn(w.conn, "connect failed");
    // Autocommit stays at the server default (ON). WL1 wraps its load in a
    // single explicit transaction; WL3 lets each INSERT autocommit.
}

static MYSQL_STMT *prepare(MYSQL *conn, const char *sql) {
    MYSQL_STMT *st = mysql_stmt_init(conn);
    if (!st)
        die_conn(conn, "mysql_stmt_init failed");
    if (mysql_stmt_prepare(st, sql, static_cast<unsigned long>(std::strlen(sql))))
        die_stmt(st, "prepare failed");
    return st;
}

// ---- MD5 (OpenSSL low-level API, consumed into the sink) --------------------

static void md5_to_sink(const unsigned char *data, size_t len) {
    unsigned char digest[MD5_DIGEST_LENGTH];
    MD5(data, len, digest);
    // Build the hex digest (SPEC: "hex digest consumed into a sink") and fold
    // it into the black box so neither the MD5 nor the hex step is elided.
    char hex[2 * MD5_DIGEST_LENGTH + 1];
    static const char *H = "0123456789abcdef";
    for (int i = 0; i < MD5_DIGEST_LENGTH; i++) {
        hex[2 * i] = H[digest[i] >> 4];
        hex[2 * i + 1] = H[digest[i] & 0x0F];
    }
    hex[2 * MD5_DIGEST_LENGTH] = '\0';
    uint64_t fold = 1469598103934665603ULL; // FNV-1a over the hex string
    for (int i = 0; i < 2 * MD5_DIGEST_LENGTH; i++) {
        fold ^= static_cast<unsigned char>(hex[i]);
        fold *= 1099511628211ULL;
    }
    black_box(fold);
}

// ---- workloads --------------------------------------------------------------
// Each function prepares its statement(s), waits at the start barrier (so
// connect + prepare are outside the timed window), then runs the timed loop.
// Per SPEC, the latency sample for one operation times the database statement
// it names ("per executed INSERT statement", "whole scan", "per blob insert",
// "per blob read"); app-side work (byte generation, MD5, the running sum that
// is inseparable from the fetch loop) is still counted in elapsed_s.

static void wl1_int_insert(Worker &w) {
    const int batch = static_cast<int>(g_cfg.int_batch);
    const long inserts = g_cfg.int_rows / g_cfg.int_batch; // rows/batch per stmt

    std::string sql = "INSERT INTO bench_int (val) VALUES ";
    for (int j = 0; j < batch; j++)
        sql += (j == 0) ? "(?)" : ",(?)";
    MYSQL_STMT *st = prepare(w.conn, sql.c_str());

    std::vector<int32_t> vals(static_cast<size_t>(batch), 0);
    std::vector<MYSQL_BIND> p(static_cast<size_t>(batch));
    std::memset(p.data(), 0, p.size() * sizeof(MYSQL_BIND));
    for (int j = 0; j < batch; j++) {
        p[j].buffer_type = MYSQL_TYPE_LONG; // 4-byte signed int
        p[j].buffer = &vals[static_cast<size_t>(j)];
        p[j].is_unsigned = 0;
    }
    if (mysql_stmt_bind_param(st, p.data()))
        die_stmt(st, "bind params (int insert) failed");

    w.lat.reserve(static_cast<size_t>(inserts));

    pthread_barrier_wait(&g_start_barrier);

    // Autocommit ON (server default): each multi-row INSERT is its own commit.
    // SPEC forbids wrapping the load in a single BEGIN/COMMIT transaction.
    long rows_done = 0;
    for (long it = 0; it < inserts; it++) {
        for (int j = 0; j < batch; j++) // fresh next() cast to signed 32-bit
            vals[static_cast<size_t>(j)] = static_cast<int32_t>(w.next());
        clock_t_::time_point t0 = clock_t_::now();
        if (mysql_stmt_execute(st))
            die_stmt(st, "int insert failed");
        w.lat.push_back(us_since(t0));
        rows_done += batch;
        w.progress.store(rows_done, std::memory_order_relaxed);
    }

    mysql_stmt_close(st);
}

static void wl2_int_read(Worker &w) {
    std::string sql =
        "SELECT val FROM bench_int LIMIT " + std::to_string(g_cfg.int_rows);
    MYSQL_STMT *st = prepare(w.conn, sql.c_str());

    int32_t val = 0;
    unsigned long len = 0;
    bind_bool is_null = 0, err = 0;
    MYSQL_BIND r;
    std::memset(&r, 0, sizeof(r));
    r.buffer_type = MYSQL_TYPE_LONG;
    r.buffer = &val;
    r.is_unsigned = 0;
    r.length = &len;
    r.is_null = &is_null;
    r.error = &err;
    if (mysql_stmt_bind_result(st, &r))
        die_stmt(st, "bind result (int read) failed");

    w.lat.reserve(1);

    pthread_barrier_wait(&g_start_barrier);

    clock_t_::time_point t0 = clock_t_::now();
    if (mysql_stmt_execute(st))
        die_stmt(st, "int read execute failed");

    int64_t sum = 0;
    long rows = 0;
    int rc;
    while ((rc = mysql_stmt_fetch(st)) == 0 || rc == MYSQL_DATA_TRUNCATED) {
        sum += val; // 64-bit accumulator, fed to the black box below
        rows++;
        if ((rows & 0x3FFF) == 0) // publish progress roughly every 16384 rows
            w.progress.store(rows, std::memory_order_relaxed);
    }
    if (rc != MYSQL_NO_DATA)
        die_stmt(st, "int read fetch failed");

    w.lat.push_back(us_since(t0));
    black_box(static_cast<uint64_t>(sum));
    w.progress.store(rows, std::memory_order_relaxed);

    mysql_stmt_close(st);
}

static void wl3_blob_insert(Worker &w) {
    const size_t sz = static_cast<size_t>(g_cfg.blob_size);
    MYSQL_STMT *st =
        prepare(w.conn, "INSERT INTO bench_blob (id, data) VALUES (?, ?)");

    long long id_val = 0;
    std::vector<unsigned char> buf(sz);
    unsigned long data_len = static_cast<unsigned long>(sz);
    MYSQL_BIND p[2];
    std::memset(p, 0, sizeof(p));
    p[0].buffer_type = MYSQL_TYPE_LONGLONG;
    p[0].buffer = &id_val;
    p[0].is_unsigned = 0;
    p[1].buffer_type = MYSQL_TYPE_LONG_BLOB;
    p[1].buffer = buf.data();
    p[1].buffer_length = static_cast<unsigned long>(sz);
    p[1].length = &data_len;
    if (mysql_stmt_bind_param(st, p))
        die_stmt(st, "bind params (blob insert) failed");

    // Fill the buffer ONCE per worker with a fast bulk LCG fill (8 bytes per
    // next() step, mixed so the bits spread across the whole word), NOT a
    // per-byte loop. Before each blob we overwrite the first 8 bytes with the
    // blob id so every blob's content (and its MD5) differs.
    {
        const size_t n8 = sz / 8;
        unsigned char *p8 = buf.data();
        for (size_t c = 0; c < n8; c++) {
            uint64_t r = w.next() * 0x9E3779B97F4A7C15ULL; // spread 31 bits
            std::memcpy(p8 + c * 8, &r, 8);
        }
        for (size_t b = n8 * 8; b < sz; b++) // tail (sz not a multiple of 8)
            buf[b] = static_cast<unsigned char>(w.next() & 0xFFu);
    }

    w.lat.reserve(static_cast<size_t>(g_cfg.blob_count));

    pthread_barrier_wait(&g_start_barrier);

    // Autocommit ON (server default): each blob INSERT is its own commit; no
    // wrapping transaction (same rule as WL1).
    for (long k = 0; k < g_cfg.blob_count; k++) {
        id_val = static_cast<long long>(w.index) * g_cfg.blob_count + k;
        std::memcpy(buf.data(), &id_val,
                    std::min(sizeof(id_val), sz)); // id into first 8 bytes
        md5_to_sink(buf.data(), sz); // MD5 of the whole buffer in app code
        data_len = static_cast<unsigned long>(sz);
        clock_t_::time_point t0 = clock_t_::now();
        if (mysql_stmt_execute(st))
            die_stmt(st, "blob insert failed");
        w.lat.push_back(us_since(t0));
        w.progress.store(k + 1, std::memory_order_relaxed);
    }

    mysql_stmt_close(st);
}

static void wl4_blob_read(Worker &w) {
    const size_t sz = static_cast<size_t>(g_cfg.blob_size);
    MYSQL_STMT *st = prepare(w.conn, "SELECT data FROM bench_blob WHERE id = ?");

    long long id_val = 0;
    MYSQL_BIND p;
    std::memset(&p, 0, sizeof(p));
    p.buffer_type = MYSQL_TYPE_LONGLONG;
    p.buffer = &id_val;
    p.is_unsigned = 0;
    if (mysql_stmt_bind_param(st, &p))
        die_stmt(st, "bind param (blob read) failed");

    std::vector<unsigned char> buf(sz);
    unsigned long data_len = 0;
    bind_bool is_null = 0, err = 0;
    MYSQL_BIND r;
    std::memset(&r, 0, sizeof(r));
    r.buffer_type = MYSQL_TYPE_LONG_BLOB;
    r.buffer = buf.data();
    r.buffer_length = static_cast<unsigned long>(sz);
    r.length = &data_len;
    r.is_null = &is_null;
    r.error = &err;
    if (mysql_stmt_bind_result(st, &r))
        die_stmt(st, "bind result (blob read) failed");

    w.lat.reserve(static_cast<size_t>(g_cfg.blob_count));

    pthread_barrier_wait(&g_start_barrier);

    for (long k = 0; k < g_cfg.blob_count; k++) {
        id_val = static_cast<long long>(w.index) * g_cfg.blob_count + k;
        data_len = 0;
        clock_t_::time_point t0 = clock_t_::now();
        if (mysql_stmt_execute(st))
            die_stmt(st, "blob read execute failed");
        int rc = mysql_stmt_fetch(st);
        if (rc == MYSQL_NO_DATA)
            die("blob read: missing row for id");
        if (rc != 0 && rc != MYSQL_DATA_TRUNCATED)
            die_stmt(st, "blob read fetch failed");
        unsigned long got = data_len;
        if (got > sz) // bound buffer is sized to BENCH_BLOB_SIZE exactly
            die("blob read: row larger than BENCH_BLOB_SIZE");
        // Drain any trailing rows (ids are unique, so normally none).
        while ((rc = mysql_stmt_fetch(st)) == 0 || rc == MYSQL_DATA_TRUNCATED) {
        }
        if (rc != MYSQL_NO_DATA)
            die_stmt(st, "blob read drain failed");
        w.lat.push_back(us_since(t0));
        md5_to_sink(buf.data(), static_cast<size_t>(got)); // MD5 in app code
        w.progress.store(k + 1, std::memory_order_relaxed);
    }

    mysql_stmt_close(st);
}

// ---- worker thread ----------------------------------------------------------

static void worker_main(Worker *wp) {
    Worker &w = *wp;
    worker_connect(w);

    switch (g_cfg.workload) {
    case 1: wl1_int_insert(w); break;
    case 2: wl2_int_read(w); break;
    case 3: wl3_blob_insert(w); break;
    case 4: wl4_blob_read(w); break;
    default: die("BENCH_WORKLOAD must be 1..4");
    }

    mysql_close(w.conn);
    mysql_thread_end();
}

// ---- workload metadata ------------------------------------------------------

static const char *workload_name(int64_t wl) {
    switch (wl) {
    case 1: return "int-insert";
    case 2: return "int-read";
    case 3: return "blob-insert";
    case 4: return "blob-read";
    default: return "unknown";
    }
}

static const char *workload_unit(int64_t wl) {
    return (wl == 3 || wl == 4) ? "blobs/s" : "rows/s";
}

static bool workload_is_blob(int64_t wl) { return wl == 3 || wl == 4; }

// ---- progress file (atomic tmp + rename) ------------------------------------

static void write_progress(const std::vector<Worker> &workers,
                           clock_t_::time_point t_start) {
    long ops = 0;
    for (const Worker &w : workers)
        ops += w.progress.load(std::memory_order_relaxed);
    double elapsed = std::chrono::duration<double>(clock_t_::now() - t_start).count();
    double thr = elapsed > 0.0 ? static_cast<double>(ops) / elapsed : 0.0;

    const std::string tmp = g_cfg.progress_file + ".tmp";
    FILE *f = std::fopen(tmp.c_str(), "w");
    if (!f)
        return; // progress is best-effort; never fail the benchmark over it
    std::fprintf(f,
                 "{\"workload\":%" PRId64 ",\"workload_name\":\"%s\","
                 "\"unit\":\"%s\",\"threads\":%" PRId64 ",\"ops\":%ld,"
                 "\"elapsed_s\":%.3f,\"throughput\":%.1f,\"partial\":true}\n",
                 g_cfg.workload, workload_name(g_cfg.workload),
                 workload_unit(g_cfg.workload), g_cfg.threads, ops, elapsed,
                 thr);
    std::fclose(f);
    (void)std::rename(tmp.c_str(), g_cfg.progress_file.c_str());
}

// ---- helpers ----------------------------------------------------------------

static std::string compiler_version() {
    // __VERSION__ on g++ is "X.Y.Z" possibly followed by vendor info; keep the
    // first token only.
    std::string v = __VERSION__;
    std::string::size_type sp = v.find(' ');
    if (sp != std::string::npos)
        v.resize(sp);
    return "g++ " + v;
}

int main() {
    // ---- configuration (environment only) ----------------------------------
    g_cfg.host = env_str("MYSQL_HOST");
    g_cfg.port = static_cast<unsigned int>(env_int("MYSQL_PORT"));
    g_cfg.user = env_str("MYSQL_USER");
    g_cfg.password = env_str("MYSQL_PASSWORD");
    g_cfg.db = env_str("MYSQL_DB");

    g_cfg.workload = env_int("BENCH_WORKLOAD");
    g_cfg.threads = env_int("BENCH_THREADS");
    g_cfg.seed = env_int("BENCH_SEED");
    g_cfg.int_rows = env_int_or("BENCH_INT_ROWS", 1000000);
    g_cfg.int_batch = env_int_or("BENCH_INT_BATCH", 10);
    g_cfg.blob_count = env_int_or("BENCH_BLOB_COUNT", 100);
    g_cfg.blob_size = env_int_or("BENCH_BLOB_SIZE", 1048576);
    g_cfg.progress_file = env_str_or("BENCH_PROGRESS_FILE", "");
    g_cfg.progress_sec = env_int_or("BENCH_PROGRESS_SEC", 5);
    if (g_cfg.progress_sec < 1)
        g_cfg.progress_sec = 1;

    if (g_cfg.workload < 1 || g_cfg.workload > 4)
        die("BENCH_WORKLOAD must be 1..4");
    if (g_cfg.threads <= 0)
        die("BENCH_THREADS must be > 0");
    if ((g_cfg.workload == 1 || g_cfg.workload == 2) && g_cfg.int_rows <= 0)
        die("BENCH_INT_ROWS must be > 0");
    if (g_cfg.workload == 1 &&
        (g_cfg.int_batch <= 0 || g_cfg.int_rows % g_cfg.int_batch != 0))
        die("BENCH_INT_BATCH must be > 0 and divide BENCH_INT_ROWS");
    if ((g_cfg.workload == 3 || g_cfg.workload == 4) &&
        (g_cfg.blob_count <= 0 || g_cfg.blob_size <= 0))
        die("BENCH_BLOB_COUNT and BENCH_BLOB_SIZE must be > 0");

    // ---- library + workers --------------------------------------------------
    if (mysql_library_init(0, nullptr, nullptr) != 0)
        die("mysql_library_init failed");

    const unsigned int nthreads = static_cast<unsigned int>(g_cfg.threads);
    if (pthread_barrier_init(&g_start_barrier, nullptr, nthreads + 1) != 0)
        die("pthread_barrier_init failed");

    // Sized once and never resized: worker addresses (and their atomics /
    // latency vectors) stay stable for the threads that reference them.
    std::vector<Worker> workers(static_cast<size_t>(g_cfg.threads));
    for (int64_t i = 0; i < g_cfg.threads; i++) {
        Worker &w = workers[static_cast<size_t>(i)];
        w.index = static_cast<int>(i);
        w.lcg = seed31(g_cfg.seed + i); // independent stream per worker
    }

    std::vector<std::thread> threads;
    threads.reserve(static_cast<size_t>(g_cfg.threads));
    for (int64_t i = 0; i < g_cfg.threads; i++)
        threads.emplace_back(worker_main, &workers[static_cast<size_t>(i)]);

    // Workers connect, prepare, then park at the barrier. Our own wait is the
    // (threads+1)-th arrival: returning from it releases them.
    pthread_barrier_wait(&g_start_barrier);
    const clock_t_::time_point t_start = clock_t_::now();

    // ---- progress monitor thread (optional) ---------------------------------
    std::mutex mon_mtx;
    std::condition_variable mon_cv;
    bool mon_stop = false;
    std::thread monitor;
    if (!g_cfg.progress_file.empty()) {
        monitor = std::thread([&] {
            std::unique_lock<std::mutex> lk(mon_mtx);
            const auto period = std::chrono::seconds(g_cfg.progress_sec);
            while (!mon_stop) {
                if (mon_cv.wait_for(lk, period, [&] { return mon_stop; }))
                    break; // stop signaled
                lk.unlock();
                write_progress(workers, t_start); // timed-out: checkpoint
                lk.lock();
            }
        });
    }

    for (auto &t : threads)
        t.join();
    const clock_t_::time_point t_end = clock_t_::now();

    if (monitor.joinable()) {
        {
            std::lock_guard<std::mutex> lk(mon_mtx);
            mon_stop = true;
        }
        mon_cv.notify_all();
        monitor.join();
    }

    pthread_barrier_destroy(&g_start_barrier);

    const double elapsed_s =
        std::chrono::duration<double>(t_end - t_start).count();

    // ---- merge + statistics -------------------------------------------------
    long ops = 0; // completed operations (rows WL1/2, blobs WL3/4)
    for (const Worker &w : workers)
        ops += w.progress.load(std::memory_order_relaxed);

    std::vector<uint64_t> lat;
    size_t total = 0;
    for (const Worker &w : workers)
        total += w.lat.size();
    lat.reserve(total);
    for (const Worker &w : workers)
        lat.insert(lat.end(), w.lat.begin(), w.lat.end());
    std::sort(lat.begin(), lat.end());

    const int64_t n = static_cast<int64_t>(lat.size());
    auto pct = [&](int64_t x) -> uint64_t {
        if (n == 0)
            return 0;
        int64_t idx = (x * n + 99) / 100 - 1; // p(x) = arr[ceil(x/100*n)-1]
        if (idx < 0)
            idx = 0;
        return lat[static_cast<size_t>(idx)];
    };
    const uint64_t p50 = pct(50), p95 = pct(95), p99 = pct(99);
    const uint64_t max_us = n > 0 ? lat[static_cast<size_t>(n - 1)] : 0;

    const double throughput =
        elapsed_s > 0.0 ? static_cast<double>(ops) / elapsed_s : 0.0;

    // mb_per_s: blob workloads only; int workloads report 0 (per SPEC example).
    char mb_buf[32];
    if (workload_is_blob(g_cfg.workload)) {
        double mb = elapsed_s > 0.0
                        ? static_cast<double>(ops) *
                              static_cast<double>(g_cfg.blob_size) / 1e6 /
                              elapsed_s
                        : 0.0;
        std::snprintf(mb_buf, sizeof(mb_buf), "%.1f", mb);
    } else {
        std::snprintf(mb_buf, sizeof(mb_buf), "0");
    }

    // ---- metadata -----------------------------------------------------------
    const char *driver_version = mysql_get_client_info();
    const std::string runtime_version = compiler_version();
    char client_host[256];
    if (gethostname(client_host, sizeof(client_host)) != 0)
        std::snprintf(client_host, sizeof(client_host), "unknown");
    client_host[sizeof(client_host) - 1] = '\0';

    // ---- one JSON line on stdout --------------------------------------------
    std::printf(
        "{\"lang\":\"cpp\",\"workload\":%" PRId64 ",\"workload_name\":\"%s\","
        "\"unit\":\"%s\",\"driver\":\"libmysqlclient\",\"driver_version\":\"%s\","
        "\"runtime_version\":\"%s\",\"prepared\":true,\"client_host\":\"%s\","
        "\"db_host\":\"%s\",\"threads\":%" PRId64 ",\"ops\":%ld,\"errors\":0,"
        "\"elapsed_s\":%.3f,\"throughput\":%.1f,\"mb_per_s\":%s,"
        "\"p50_us\":%" PRIu64 ",\"p95_us\":%" PRIu64 ",\"p99_us\":%" PRIu64
        ",\"max_us\":%" PRIu64 ",\"client_cpu_s\":0,\"server_cpu_s\":0}\n",
        g_cfg.workload, workload_name(g_cfg.workload),
        workload_unit(g_cfg.workload), driver_version, runtime_version.c_str(),
        client_host, g_cfg.host, g_cfg.threads, ops, elapsed_s, throughput,
        mb_buf, p50, p95, p99, max_us);

    // Progress data is now superseded by stdout: delete the checkpoint.
    if (!g_cfg.progress_file.empty())
        (void)std::remove(g_cfg.progress_file.c_str());

    mysql_library_end();
    return 0;
}
