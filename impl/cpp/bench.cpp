// mysql-lang-bench: C++ implementation (libmysqlclient C API, server-side
// prepared statements, std::thread workers). Follows SPEC.md (v2,
// multithreaded oltp_read_write) exactly.
//
// Build: c++ -std=c++17 -O2 -pthread bench.cpp -o bench
//        $(mysql_config --cflags --libs)

#include <mysql.h>

#include <pthread.h> // pthread_barrier_t (C++17 has no std::barrier)
#include <unistd.h>  // gethostname

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cinttypes>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>
#include <type_traits>
#include <vector>

// MYSQL_BIND::is_null / ::error are `my_bool*` on older clients and `bool*`
// on MySQL >= 8.0.1; derive the element type portably from the header.
using bind_bool = std::remove_pointer_t<decltype(MYSQL_BIND::is_null)>;

// die* may be called from worker threads: std::exit terminates the whole
// process, which is exactly what SPEC.md wants for non-retryable errors.
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

static int64_t env_int(const char *name) {
    return parse_int(name, env_str(name));
}

static int64_t env_int_or(const char *name, int64_t def) {
    const char *v = std::getenv(name);
    if (!v || !*v)
        return def;
    return parse_int(name, v);
}

// ---- configuration (written once in main, before any thread starts) --------

struct Config {
    const char *host = nullptr;
    const char *user = nullptr;
    const char *password = nullptr;
    const char *db = nullptr;
    unsigned int port = 0;
    int64_t table_rows = 0;
    int64_t threads = 0;
    int64_t warmup_txns = 0;
    int64_t txns_per_thread = 0;
    int64_t duration_s = 0; // 0 = events mode, > 0 = fixed-duration mode
    int64_t range_size = 0;
    int64_t seed = 0;
};

static Config g_cfg;
static std::string C_VAL;   // "abcdefghij" x 12 (120 chars)
static std::string PAD_VAL; // "0123456789" x 6  (60 chars)
static pthread_barrier_t g_start_barrier;

// ---- the 18 per-worker prepared statements ---------------------------------
// Slots: 0..9 point selects, 10 simple range, 11 sum range, 12 order range,
// 13 distinct range, 14 index update, 15 non-index update, 16 delete,
// 17 insert.

static const char *SQL_POINT = "SELECT c FROM sbtest WHERE id = ?";
static const char *SQL_RANGE = "SELECT c FROM sbtest WHERE id BETWEEN ? AND ?";
static const char *SQL_SUM = "SELECT SUM(k) FROM sbtest WHERE id BETWEEN ? AND ?";
static const char *SQL_ORDER =
    "SELECT c FROM sbtest WHERE id BETWEEN ? AND ? ORDER BY c";
static const char *SQL_DISTINCT =
    "SELECT DISTINCT c FROM sbtest WHERE id BETWEEN ? AND ? ORDER BY c";
static const char *SQL_UPD_IDX = "UPDATE sbtest SET k = k + 1 WHERE id = ?";
static const char *SQL_UPD_NONIDX = "UPDATE sbtest SET c = ? WHERE id = ?";
static const char *SQL_DELETE = "DELETE FROM sbtest WHERE id = ?";
static const char *SQL_INSERT =
    "INSERT INTO sbtest (id, k, c, pad) VALUES (?, ?, ?, ?)";

// ---- per-worker state -------------------------------------------------------
// Workers share no mutable state; results are read by main only after join.

struct Worker {
    int index = 0;
    MYSQL *conn = nullptr;
    MYSQL_STMT *st[18] = {};

    uint64_t lcg = 0;      // 31-bit LCG state, seeded with BENCH_SEED + index
    uint64_t retries = 0;  // timed-window retries (reset after warmup)
    std::vector<uint64_t> lat; // reserved in main, filled with push_back

    // Bound parameter values (stable addresses for MYSQL_BIND buffers).
    long long point_id[10] = {};
    long long range_lo[4] = {};
    long long range_hi[4] = {};
    long long upd_idx_id = 0;
    long long upd_nonidx_id = 0;
    long long del_ins_id = 0; // same id for DELETE and the following INSERT
    long long ins_k = 0;
    unsigned long cval_len = 0;
    unsigned long pad_len = 0;

    // Bound result buffers. Statements execute one at a time within a worker,
    // so the `c` buffer is safely shared by every select that returns it.
    char c_buf[256];
    unsigned long c_len = 0;
    bind_bool c_null = 0;
    bind_bool c_err = 0;
    // SUM(k) over INT UNSIGNED yields DECIMAL; fetch it as a string.
    char sum_buf[64];
    unsigned long sum_len = 0;
    bind_bool sum_null = 0;
    bind_bool sum_err = 0;

    // Bind descriptors, kept alive for the worker's lifetime.
    MYSQL_BIND pbind[18][4]; // params (max 4: the INSERT)
    MYSQL_BIND rbind[18][1]; // results (every select returns one column)
};

// ---- PRNG (31-bit LCG, identical in every language) -------------------------
// state = (state * 1103515245 + 12345) mod 2^31. Worker i seeds BENCH_SEED+i.
// Validation (seed 42): next() = 1250496027, 1116302264, 1000676753,
// 1668674806, 908095735; rand(100) = 27, 64, 53, 6, 35.

static inline uint64_t lcg_next(Worker &w) {
    w.lcg = (w.lcg * 1103515245ULL + 12345ULL) & 0x7FFFFFFFULL;
    return w.lcg;
}

static inline int64_t lcg_rand(Worker &w, int64_t n) {
    return static_cast<int64_t>(lcg_next(w) % static_cast<uint64_t>(n));
}

static uint64_t seed31(int64_t s) {
    int64_t m = ((s % 2147483648LL) + 2147483648LL) % 2147483648LL;
    return static_cast<uint64_t>(m);
}

// ---- connect + prepare ------------------------------------------------------

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
    // Autocommit stays at the server default (ON); transactions are explicit
    // BEGIN/COMMIT plain queries.
}

static void set_ll(MYSQL_BIND &b, long long *v) {
    b.buffer_type = MYSQL_TYPE_LONGLONG;
    b.buffer = v;
}

static void set_str(MYSQL_BIND &b, const char *s, unsigned long *len) {
    b.buffer_type = MYSQL_TYPE_STRING;
    b.buffer = const_cast<char *>(s);
    b.buffer_length = *len;
    b.length = len;
}

static void worker_prepare(Worker &w) {
    std::memset(w.pbind, 0, sizeof(w.pbind));
    std::memset(w.rbind, 0, sizeof(w.rbind));
    w.cval_len = static_cast<unsigned long>(C_VAL.size());
    w.pad_len = static_cast<unsigned long>(PAD_VAL.size());

    auto prep = [&](int slot, const char *sql) {
        w.st[slot] = mysql_stmt_init(w.conn);
        if (!w.st[slot])
            die_conn(w.conn, "mysql_stmt_init failed");
        if (mysql_stmt_prepare(w.st[slot], sql, std::strlen(sql)) != 0)
            die_stmt(w.st[slot], "prepare failed");
    };
    auto bind_params = [&](int slot) {
        if (mysql_stmt_bind_param(w.st[slot], w.pbind[slot]) != 0)
            die_stmt(w.st[slot], "bind params failed");
    };
    auto bind_result = [&](int slot, char *buf, unsigned long buf_len,
                           unsigned long *len, bind_bool *is_null,
                           bind_bool *err) {
        MYSQL_BIND &r = w.rbind[slot][0];
        r.buffer_type = MYSQL_TYPE_STRING;
        r.buffer = buf;
        r.buffer_length = buf_len;
        r.length = len;
        r.is_null = is_null;
        r.error = err;
        if (mysql_stmt_bind_result(w.st[slot], w.rbind[slot]) != 0)
            die_stmt(w.st[slot], "bind result failed");
    };
    auto bind_c_result = [&](int slot) {
        bind_result(slot, w.c_buf, sizeof(w.c_buf), &w.c_len, &w.c_null,
                    &w.c_err);
    };

    for (int j = 0; j < 10; j++) { // slots 0..9: point selects
        prep(j, SQL_POINT);
        set_ll(w.pbind[j][0], &w.point_id[j]);
        bind_params(j);
        bind_c_result(j);
    }

    const char *range_sql[4] = {SQL_RANGE, SQL_SUM, SQL_ORDER, SQL_DISTINCT};
    for (int j = 0; j < 4; j++) { // slots 10..13: range selects
        int slot = 10 + j;
        prep(slot, range_sql[j]);
        set_ll(w.pbind[slot][0], &w.range_lo[j]);
        set_ll(w.pbind[slot][1], &w.range_hi[j]);
        bind_params(slot);
        if (slot == 11)
            bind_result(slot, w.sum_buf, sizeof(w.sum_buf), &w.sum_len,
                        &w.sum_null, &w.sum_err);
        else
            bind_c_result(slot);
    }

    prep(14, SQL_UPD_IDX);
    set_ll(w.pbind[14][0], &w.upd_idx_id);
    bind_params(14);

    prep(15, SQL_UPD_NONIDX);
    set_str(w.pbind[15][0], C_VAL.c_str(), &w.cval_len);
    set_ll(w.pbind[15][1], &w.upd_nonidx_id);
    bind_params(15);

    prep(16, SQL_DELETE);
    set_ll(w.pbind[16][0], &w.del_ins_id);
    bind_params(16);

    prep(17, SQL_INSERT);
    set_ll(w.pbind[17][0], &w.del_ins_id);
    set_ll(w.pbind[17][1], &w.ins_k);
    set_str(w.pbind[17][2], C_VAL.c_str(), &w.cval_len);
    set_str(w.pbind[17][3], PAD_VAL.c_str(), &w.pad_len);
    bind_params(17);
}

// ---- one transaction --------------------------------------------------------

static inline bool retryable(unsigned int e) {
    return e == 1213 /* deadlock */ || e == 1205 /* lock wait timeout */ ||
           e == 1062 /* duplicate key */;
}

static void rollback_ignore(Worker &w) {
    (void)mysql_query(w.conn, "ROLLBACK"); // errors deliberately ignored
}

// Executes one prepared statement; fetches ALL rows when has_result.
// Returns true on success, false on a retryable concurrency error
// (1213/1205/1062). Any other error aborts the process.
static bool exec_stmt(Worker &w, int slot, bool has_result, const char *what) {
    MYSQL_STMT *s = w.st[slot];
    if (mysql_stmt_execute(s) != 0) {
        if (retryable(mysql_stmt_errno(s)))
            return false;
        die_stmt(s, what);
    }
    if (has_result) {
        int rc;
        while ((rc = mysql_stmt_fetch(s)) == 0 || rc == MYSQL_DATA_TRUNCATED) {
            // row fetched (full result row read into the bound buffers)
        }
        if (rc != MYSQL_NO_DATA) {
            unsigned int e = mysql_stmt_errno(s);
            mysql_stmt_free_result(s); // reset the statement before ROLLBACK
            if (retryable(e))
                return false;
            die_stmt(s, what);
        }
    }
    return true;
}

// Runs the transaction body once. Values are drawn immediately before each
// statement executes (draw order = execution order). Returns true when
// committed; false on a retryable error, with ROLLBACK already issued.
static bool txn_once(Worker &w) {
    const int64_t N = g_cfg.table_rows;
    const int64_t R = g_cfg.range_size;

    if (mysql_query(w.conn, "BEGIN") != 0) {
        if (!retryable(mysql_errno(w.conn)))
            die_conn(w.conn, "BEGIN failed");
        rollback_ignore(w);
        return false;
    }

    for (int j = 0; j < 10; j++) { // 10 point selects
        w.point_id[j] = 1 + lcg_rand(w, N);
        if (!exec_stmt(w, j, true, "point select failed")) {
            rollback_ignore(w);
            return false;
        }
    }

    const char *range_what[4] = {"simple range failed", "sum range failed",
                                 "order range failed", "distinct range failed"};
    for (int j = 0; j < 4; j++) { // 4 range selects (no clamping past N)
        w.range_lo[j] = 1 + lcg_rand(w, N);
        w.range_hi[j] = w.range_lo[j] + R - 1;
        if (!exec_stmt(w, 10 + j, true, range_what[j])) {
            rollback_ignore(w);
            return false;
        }
    }

    w.upd_idx_id = 1 + lcg_rand(w, N); // index update
    if (!exec_stmt(w, 14, false, "index update failed")) {
        rollback_ignore(w);
        return false;
    }

    w.upd_nonidx_id = 1 + lcg_rand(w, N); // non-index update
    if (!exec_stmt(w, 15, false, "non-index update failed")) {
        rollback_ignore(w);
        return false;
    }

    w.del_ins_id = 1 + lcg_rand(w, N); // delete+insert share the id
    w.ins_k = lcg_rand(w, N);
    if (!exec_stmt(w, 16, false, "delete failed")) {
        rollback_ignore(w);
        return false;
    }
    if (!exec_stmt(w, 17, false, "insert failed")) {
        rollback_ignore(w);
        return false;
    }

    if (mysql_query(w.conn, "COMMIT") != 0) {
        if (!retryable(mysql_errno(w.conn)))
            die_conn(w.conn, "COMMIT failed");
        rollback_ignore(w);
        return false;
    }
    return true;
}

// Retries until the transaction commits; the PRNG stream simply continues,
// so every retry redraws all values.
static void run_txn(Worker &w) {
    while (!txn_once(w))
        w.retries++;
}

// ---- worker thread ----------------------------------------------------------

static void worker_main(Worker *wp) {
    Worker &w = *wp;

    worker_connect(w);
    worker_prepare(w);

    for (int64_t i = 0; i < g_cfg.warmup_txns; i++) // untimed warmup
        run_txn(w);
    w.retries = 0; // report retries from the timed window only

    pthread_barrier_wait(&g_start_barrier);

    using clock = std::chrono::steady_clock;
    auto timed_txn = [&w]() {
        clock::time_point t0 = clock::now(); // just before the first BEGIN
        run_txn(w);                          // retry time included
        w.lat.push_back(static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::microseconds>(clock::now() -
                                                                  t0)
                .count())); // integer microseconds, truncated
    };
    if (g_cfg.duration_s > 0) {
        // Duration mode: deadline checked BEFORE each new transaction; a
        // transaction in flight at the deadline (retries included) completes.
        const clock::time_point deadline =
            clock::now() + std::chrono::seconds(g_cfg.duration_s);
        while (clock::now() < deadline)
            timed_txn();
    } else {
        for (int64_t i = 0; i < g_cfg.txns_per_thread; i++) // events mode
            timed_txn();
    }

    for (int j = 0; j < 18; j++)
        mysql_stmt_close(w.st[j]);
    mysql_close(w.conn);
    mysql_thread_end();
}

// ---- helpers ----------------------------------------------------------------

static std::string compiler_version() {
    // __VERSION__ on g++ is "X.Y.Z" possibly followed by vendor info;
    // keep the first token only.
    std::string v = __VERSION__;
    std::string::size_type sp = v.find(' ');
    if (sp != std::string::npos)
        v.resize(sp);
    return "g++ " + v;
}

int main() {
    // ---- configuration (environment only) ----------------------------------
    g_cfg.host = env_str("MYSQL_HOST");
    int64_t port = env_int("MYSQL_PORT");
    g_cfg.user = env_str("MYSQL_USER");
    g_cfg.password = env_str("MYSQL_PASSWORD");
    g_cfg.db = env_str("MYSQL_DB");
    g_cfg.port = static_cast<unsigned int>(port);

    g_cfg.table_rows = env_int("BENCH_TABLE_ROWS");
    g_cfg.threads = env_int("BENCH_THREADS");
    g_cfg.warmup_txns = env_int("BENCH_WARMUP_TXNS");
    g_cfg.duration_s = env_int_or("BENCH_DURATION_S", 0);
    // Duration mode ignores BENCH_TXNS_PER_THREAD (it need not be set);
    // events mode requires it, exactly as before.
    g_cfg.txns_per_thread = g_cfg.duration_s > 0
                                ? env_int_or("BENCH_TXNS_PER_THREAD", 0)
                                : env_int("BENCH_TXNS_PER_THREAD");
    g_cfg.range_size = env_int_or("BENCH_RANGE_SIZE", 100);
    g_cfg.seed = env_int("BENCH_SEED");

    if (g_cfg.table_rows <= 0 || g_cfg.threads <= 0 ||
        (g_cfg.duration_s <= 0 && g_cfg.txns_per_thread <= 0) ||
        g_cfg.warmup_txns < 0 || g_cfg.range_size <= 0 || g_cfg.duration_s < 0)
        die("BENCH_TABLE_ROWS/BENCH_THREADS/BENCH_TXNS_PER_THREAD/"
            "BENCH_RANGE_SIZE must be > 0, BENCH_WARMUP_TXNS/BENCH_DURATION_S"
            " >= 0");

    for (int i = 0; i < 12; i++)
        C_VAL += "abcdefghij"; // 120 chars
    for (int i = 0; i < 6; i++)
        PAD_VAL += "0123456789"; // 60 chars

    // ---- library + workers --------------------------------------------------
    // Must run before any thread touches the client library.
    if (mysql_library_init(0, nullptr, nullptr) != 0)
        die("mysql_library_init failed");

    const unsigned int nthreads = static_cast<unsigned int>(g_cfg.threads);
    if (pthread_barrier_init(&g_start_barrier, nullptr, nthreads + 1) != 0)
        die("pthread_barrier_init failed");

    // Constructed up front and never resized: worker addresses stay stable
    // for the bind buffers and the threads that reference them.
    std::vector<Worker> workers(static_cast<size_t>(g_cfg.threads));
    for (int64_t i = 0; i < g_cfg.threads; i++) {
        Worker &w = workers[static_cast<size_t>(i)];
        w.index = static_cast<int>(i);
        w.lcg = seed31(g_cfg.seed + i); // independent stream per worker
        // Events mode: exact size known up front. Duration mode: unknown
        // count, grow dynamically from a modest initial reservation.
        w.lat.reserve(g_cfg.duration_s > 0
                          ? 4096
                          : static_cast<size_t>(g_cfg.txns_per_thread));
    }

    std::vector<std::thread> threads;
    threads.reserve(static_cast<size_t>(g_cfg.threads));
    for (int64_t i = 0; i < g_cfg.threads; i++)
        threads.emplace_back(worker_main, &workers[static_cast<size_t>(i)]);

    // Workers connect, prepare, warm up, then park at the barrier. Our own
    // wait is the (threads+1)-th arrival: returning from it releases them.
    using clock = std::chrono::steady_clock;
    pthread_barrier_wait(&g_start_barrier);
    const clock::time_point t_start = clock::now();

    for (auto &t : threads)
        t.join();
    const clock::time_point t_end = clock::now();

    pthread_barrier_destroy(&g_start_barrier);

    const double elapsed_s =
        std::chrono::duration<double>(t_end - t_start).count();

    // ---- merge + statistics --------------------------------------------------
    // txns = actual committed timed transactions across workers (in events
    // mode this equals BENCH_THREADS * BENCH_TXNS_PER_THREAD).
    size_t total = 0;
    for (const Worker &w : workers)
        total += w.lat.size();
    const int64_t txns = static_cast<int64_t>(total);
    uint64_t retries = 0;
    std::vector<uint64_t> lat;
    lat.reserve(total);
    for (const Worker &w : workers) {
        retries += w.retries;
        lat.insert(lat.end(), w.lat.begin(), w.lat.end());
    }
    std::sort(lat.begin(), lat.end());

    const int64_t n = txns;
    auto pct = [&](int64_t x) -> uint64_t {
        // p(x) = arr[ceil(x/100 * n) - 1], 0-based
        int64_t idx = (x * n + 99) / 100 - 1;
        if (idx < 0)
            idx = 0;
        return lat[static_cast<size_t>(idx)];
    };
    const uint64_t p50 = pct(50), p95 = pct(95), p99 = pct(99);
    const uint64_t max_us = lat[static_cast<size_t>(n - 1)];
    const double tps =
        elapsed_s > 0.0 ? static_cast<double>(txns) / elapsed_s : 0.0;
    const double qps = tps * 20.0; // 18 statements + BEGIN + COMMIT

    // ---- metadata -------------------------------------------------------------
    const char *driver_version = mysql_get_client_info();
    const std::string runtime_version = compiler_version();
    char hostname[256];
    if (gethostname(hostname, sizeof(hostname)) != 0)
        std::snprintf(hostname, sizeof(hostname), "unknown");
    hostname[sizeof(hostname) - 1] = '\0';

    mysql_library_end();

    // ---- one JSON line on stdout ----------------------------------------------
    std::printf("{\"lang\":\"cpp\",\"driver\":\"libmysqlclient\","
                "\"driver_version\":\"%s\",\"runtime_version\":\"%s\","
                "\"prepared\":true,\"client_host\":\"%s\",\"db_host\":\"%s\","
                "\"threads\":%" PRId64 ",\"txns\":%" PRId64
                ",\"retries\":%" PRIu64 ",\"errors\":0,\"elapsed_s\":%.3f,"
                "\"tps\":%.1f,\"qps\":%.1f,\"p50_us\":%" PRIu64
                ",\"p95_us\":%" PRIu64 ",\"p99_us\":%" PRIu64
                ",\"max_us\":%" PRIu64 "}\n",
                driver_version, runtime_version.c_str(), hostname, g_cfg.host,
                g_cfg.threads, txns, retries, elapsed_s, tps, qps, p50, p95,
                p99, max_us);
    return 0;
}
