/*
 * mysql-lang-bench — C implementation (v2, multithreaded oltp_read_write).
 * libmysqlclient C API with mysql_stmt_* server-side prepared statements.
 * See SPEC.md: BENCH_THREADS pthread workers, each with its own connection,
 * its own 18 prepared statements and its own PRNG stream, run a
 * sysbench-oltp_read_write-style transaction loop. One JSON line on stdout.
 */

#define _GNU_SOURCE

#include <mysql.h>

#include <inttypes.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

/* C_VAL = "abcdefghij" repeated 12 times (120 chars) */
#define C_VAL \
    "abcdefghij" "abcdefghij" "abcdefghij" "abcdefghij" \
    "abcdefghij" "abcdefghij" "abcdefghij" "abcdefghij" \
    "abcdefghij" "abcdefghij" "abcdefghij" "abcdefghij"
/* PAD_VAL = "0123456789" repeated 6 times (60 chars) */
#define PAD_VAL \
    "0123456789" "0123456789" "0123456789" \
    "0123456789" "0123456789" "0123456789"

#if defined(__clang__)
#define RUNTIME_VERSION "clang " __clang_version__
#elif defined(__GNUC__)
#define RUNTIME_VERSION "gcc " __VERSION__
#else
#define RUNTIME_VERSION "cc " __VERSION__
#endif

/* ---------------- error helpers ------------------------------------------ */

static void die_mysql(MYSQL *conn, const char *what)
{
    fprintf(stderr, "%s failed: (%u) %s\n", what, mysql_errno(conn),
            mysql_error(conn));
    exit(1);
}

static void die_stmt(MYSQL_STMT *stmt, const char *what)
{
    fprintf(stderr, "%s failed: (%u) %s\n", what, mysql_stmt_errno(stmt),
            mysql_stmt_error(stmt));
    exit(1);
}

/* Retryable concurrency errors per SPEC: deadlock, lock wait timeout,
 * duplicate key on the insert. */
static bool err_retryable(unsigned int e)
{
    return e == 1213 || e == 1205 || e == 1062;
}

/* ---------------- env parsing -------------------------------------------- */

static const char *env_str(const char *name)
{
    const char *v = getenv(name);
    if (v == NULL) {
        fprintf(stderr, "missing required environment variable %s\n", name);
        exit(1);
    }
    return v;
}

static long env_int(const char *name)
{
    const char *v = env_str(name);
    char *end = NULL;
    long n = strtol(v, &end, 10);
    if (end == v || *end != '\0') {
        fprintf(stderr, "environment variable %s is not an integer: '%s'\n",
                name, v);
        exit(1);
    }
    return n;
}

/* ---------------- timing -------------------------------------------------- */

static int64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + (int64_t)ts.tv_nsec;
}

/* ---------------- stats --------------------------------------------------- */

static int cmp_i64(const void *a, const void *b)
{
    int64_t x = *(const int64_t *)a;
    int64_t y = *(const int64_t *)b;
    return (x > y) - (x < y);
}

/* p(x) = arr[ceil(x/100 * n) - 1], arr sorted ascending, 0-based index */
static int64_t percentile(const int64_t *arr, long n, int x)
{
    long idx = (long)(((int64_t)x * n + 99) / 100) - 1;
    if (idx < 0)
        idx = 0;
    return arr[idx];
}

/* ---------------- shared, read-only workload config ----------------------- */

static const char *cfg_host, *cfg_user, *cfg_password, *cfg_db;
static long cfg_port;
static long table_rows, n_threads, warmup_txns, txns_per_thread, range_size;
static long base_seed;

static pthread_barrier_t start_barrier;

/* Constant string parameters, read-only after init — safe to share. */
static char c_val[] = C_VAL;
static char pad_val[] = PAD_VAL;
static unsigned long c_val_len = sizeof(c_val) - 1;     /* 120 */
static unsigned long pad_val_len = sizeof(pad_val) - 1; /* 60 */

/* ---------------- per-worker state ---------------------------------------- */

/* The 18 server-side prepared statements, one set per worker. */
enum {
    ST_POINT0 = 0,          /* 10 point selects: ST_POINT0 .. ST_POINT0+9 */
    ST_RANGE_SIMPLE = 10,
    ST_RANGE_SUM = 11,
    ST_RANGE_ORDER = 12,
    ST_RANGE_DISTINCT = 13,
    ST_UPD_INDEX = 14,
    ST_UPD_NONINDEX = 15,
    ST_DELETE = 16,
    ST_INSERT = 17,
    ST_COUNT = 18
};

typedef struct worker {
    pthread_t tid;
    int index;

    int64_t prng_state;

    MYSQL *conn;
    MYSQL_STMT *st[ST_COUNT];

    /* Parameter value buffers (bound once; values written before execute). */
    unsigned int point_id[10];
    unsigned int range_lo[4], range_hi[4]; /* simple, sum, order, distinct */
    unsigned int upd_index_id;
    unsigned int upd_nonindex_id;
    unsigned int del_id;
    unsigned int ins_id, ins_k;

    /* Result buffers. One worker runs its statements strictly sequentially,
     * so the selects returning column c can share one row buffer. */
    char row_c[512]; /* CHAR(120); up to 480 bytes as utf8mb4 */
    unsigned long row_c_len;
    char sum_buf[80]; /* SUM(k) over INT UNSIGNED arrives as DECIMAL text */
    unsigned long sum_len;
    bool sum_is_null;

    /* MYSQL_BIND arrays kept alive for the worker's whole lifetime. */
    MYSQL_BIND bp_point[10][1];
    MYSQL_BIND br_point[10][1];
    MYSQL_BIND bp_range[4][2];
    MYSQL_BIND br_range_c[3][1]; /* simple, order, distinct */
    MYSQL_BIND br_sum[1];
    MYSQL_BIND bp_upd_index[1];
    MYSQL_BIND bp_upd_nonindex[2];
    MYSQL_BIND bp_del[1];
    MYSQL_BIND bp_ins[4];

    int64_t *lat;    /* BENCH_TXNS_PER_THREAD entries, pre-allocated */
    int64_t retries;
} worker;

/* ---------------- PRNG: 31-bit LCG, identical in every language ----------- */

static int64_t prng_next(worker *w)
{
    /* (state * 1103515245 + 12345) mod 2^31; product fits in int64 */
    w->prng_state = (w->prng_state * 1103515245 + 12345) & 0x7fffffffLL;
    return w->prng_state;
}

static int64_t prng_rand(worker *w, int64_t n) /* uniform 0..n-1 */
{
    return prng_next(w) % n;
}

/* ---------------- per-worker setup ----------------------------------------- */

static MYSQL_STMT *prepare_stmt(MYSQL *conn, const char *sql)
{
    MYSQL_STMT *stmt = mysql_stmt_init(conn);
    if (stmt == NULL)
        die_mysql(conn, "mysql_stmt_init");
    if (mysql_stmt_prepare(stmt, sql, (unsigned long)strlen(sql)))
        die_stmt(stmt, "mysql_stmt_prepare");
    return stmt;
}

static void bind_uint_param(MYSQL_BIND *b, unsigned int *value)
{
    memset(b, 0, sizeof(*b));
    b->buffer_type = MYSQL_TYPE_LONG;
    b->buffer = value;
    b->is_unsigned = 1;
}

static void worker_connect(worker *w)
{
    w->conn = mysql_init(NULL);
    if (w->conn == NULL) {
        fprintf(stderr, "mysql_init failed (out of memory)\n");
        exit(1);
    }
    unsigned int proto = MYSQL_PROTOCOL_TCP;
    mysql_options(w->conn, MYSQL_OPT_PROTOCOL, &proto);
    /* Plaintext everywhere: libmysqlclient defaults to SSL_MODE_PREFERRED,
       which would put this driver on TLS while others run plaintext. */
    unsigned int ssl_mode = SSL_MODE_DISABLED;
    mysql_options(w->conn, MYSQL_OPT_SSL_MODE, &ssl_mode);
    /* caching_sha2_password full auth over plaintext needs the server's RSA key. */
    bool get_server_pk = true;
    mysql_options(w->conn, MYSQL_OPT_GET_SERVER_PUBLIC_KEY, &get_server_pk);
    if (mysql_real_connect(w->conn, cfg_host, cfg_user, cfg_password, cfg_db,
                           (unsigned int)cfg_port, NULL, 0) == NULL)
        die_mysql(w->conn, "mysql_real_connect");
    if (mysql_autocommit(w->conn, 1))
        die_mysql(w->conn, "mysql_autocommit");
}

static void worker_prepare(worker *w)
{
    MYSQL *conn = w->conn;

    /* 10 point selects, each its own prepared statement (sysbench-style). */
    for (int j = 0; j < 10; j++) {
        w->st[ST_POINT0 + j] =
            prepare_stmt(conn, "SELECT c FROM sbtest WHERE id = ?");
        bind_uint_param(&w->bp_point[j][0], &w->point_id[j]);
        if (mysql_stmt_bind_param(w->st[ST_POINT0 + j], w->bp_point[j]))
            die_stmt(w->st[ST_POINT0 + j], "mysql_stmt_bind_param(point)");

        memset(w->br_point[j], 0, sizeof(w->br_point[j]));
        w->br_point[j][0].buffer_type = MYSQL_TYPE_STRING;
        w->br_point[j][0].buffer = w->row_c;
        w->br_point[j][0].buffer_length = sizeof(w->row_c);
        w->br_point[j][0].length = &w->row_c_len;
        if (mysql_stmt_bind_result(w->st[ST_POINT0 + j], w->br_point[j]))
            die_stmt(w->st[ST_POINT0 + j], "mysql_stmt_bind_result(point)");
    }

    /* 4 range selects: params (lo, hi) each. */
    static const char *range_sql[4] = {
        "SELECT c FROM sbtest WHERE id BETWEEN ? AND ?",
        "SELECT SUM(k) FROM sbtest WHERE id BETWEEN ? AND ?",
        "SELECT c FROM sbtest WHERE id BETWEEN ? AND ? ORDER BY c",
        "SELECT DISTINCT c FROM sbtest WHERE id BETWEEN ? AND ? ORDER BY c",
    };
    for (int j = 0; j < 4; j++) {
        MYSQL_STMT *st = prepare_stmt(conn, range_sql[j]);
        w->st[ST_RANGE_SIMPLE + j] = st;
        bind_uint_param(&w->bp_range[j][0], &w->range_lo[j]);
        bind_uint_param(&w->bp_range[j][1], &w->range_hi[j]);
        if (mysql_stmt_bind_param(st, w->bp_range[j]))
            die_stmt(st, "mysql_stmt_bind_param(range)");
    }

    /* Result binds: column c for simple/order/distinct (shared row buffer). */
    {
        const int c_stmts[3] = { ST_RANGE_SIMPLE, ST_RANGE_ORDER,
                                 ST_RANGE_DISTINCT };
        for (int j = 0; j < 3; j++) {
            memset(w->br_range_c[j], 0, sizeof(w->br_range_c[j]));
            w->br_range_c[j][0].buffer_type = MYSQL_TYPE_STRING;
            w->br_range_c[j][0].buffer = w->row_c;
            w->br_range_c[j][0].buffer_length = sizeof(w->row_c);
            w->br_range_c[j][0].length = &w->row_c_len;
            if (mysql_stmt_bind_result(w->st[c_stmts[j]], w->br_range_c[j]))
                die_stmt(w->st[c_stmts[j]], "mysql_stmt_bind_result(range)");
        }
    }

    /* SUM(k) over INT UNSIGNED yields DECIMAL; fetch it as a string buffer.
       It is NULL when the range matches no rows (rows may be deleted by
       concurrent workers), so bind is_null too. */
    memset(w->br_sum, 0, sizeof(w->br_sum));
    w->br_sum[0].buffer_type = MYSQL_TYPE_NEWDECIMAL;
    w->br_sum[0].buffer = w->sum_buf;
    w->br_sum[0].buffer_length = sizeof(w->sum_buf);
    w->br_sum[0].length = &w->sum_len;
    w->br_sum[0].is_null = &w->sum_is_null;
    if (mysql_stmt_bind_result(w->st[ST_RANGE_SUM], w->br_sum))
        die_stmt(w->st[ST_RANGE_SUM], "mysql_stmt_bind_result(sum)");

    /* Index update. */
    w->st[ST_UPD_INDEX] =
        prepare_stmt(conn, "UPDATE sbtest SET k = k + 1 WHERE id = ?");
    bind_uint_param(&w->bp_upd_index[0], &w->upd_index_id);
    if (mysql_stmt_bind_param(w->st[ST_UPD_INDEX], w->bp_upd_index))
        die_stmt(w->st[ST_UPD_INDEX], "mysql_stmt_bind_param(upd_index)");

    /* Non-index update: params (c, id). */
    w->st[ST_UPD_NONINDEX] =
        prepare_stmt(conn, "UPDATE sbtest SET c = ? WHERE id = ?");
    memset(w->bp_upd_nonindex, 0, sizeof(w->bp_upd_nonindex));
    w->bp_upd_nonindex[0].buffer_type = MYSQL_TYPE_STRING;
    w->bp_upd_nonindex[0].buffer = c_val;
    w->bp_upd_nonindex[0].buffer_length = c_val_len;
    w->bp_upd_nonindex[0].length = &c_val_len;
    bind_uint_param(&w->bp_upd_nonindex[1], &w->upd_nonindex_id);
    if (mysql_stmt_bind_param(w->st[ST_UPD_NONINDEX], w->bp_upd_nonindex))
        die_stmt(w->st[ST_UPD_NONINDEX], "mysql_stmt_bind_param(upd_nonindex)");

    /* Delete. */
    w->st[ST_DELETE] = prepare_stmt(conn, "DELETE FROM sbtest WHERE id = ?");
    bind_uint_param(&w->bp_del[0], &w->del_id);
    if (mysql_stmt_bind_param(w->st[ST_DELETE], w->bp_del))
        die_stmt(w->st[ST_DELETE], "mysql_stmt_bind_param(delete)");

    /* Insert: params (id, k, c, pad). */
    w->st[ST_INSERT] = prepare_stmt(
        conn, "INSERT INTO sbtest (id, k, c, pad) VALUES (?, ?, ?, ?)");
    memset(w->bp_ins, 0, sizeof(w->bp_ins));
    bind_uint_param(&w->bp_ins[0], &w->ins_id);
    bind_uint_param(&w->bp_ins[1], &w->ins_k);
    w->bp_ins[2].buffer_type = MYSQL_TYPE_STRING;
    w->bp_ins[2].buffer = c_val;
    w->bp_ins[2].buffer_length = c_val_len;
    w->bp_ins[2].length = &c_val_len;
    w->bp_ins[3].buffer_type = MYSQL_TYPE_STRING;
    w->bp_ins[3].buffer = pad_val;
    w->bp_ins[3].buffer_length = pad_val_len;
    w->bp_ins[3].length = &pad_val_len;
    if (mysql_stmt_bind_param(w->st[ST_INSERT], w->bp_ins))
        die_stmt(w->st[ST_INSERT], "mysql_stmt_bind_param(insert)");
}

/* ---------------- transaction execution ------------------------------------ */

/* Returns 0 on success, 1 on a retryable concurrency error; dies otherwise. */
static int exec_dml(MYSQL_STMT *st, const char *what)
{
    if (mysql_stmt_execute(st)) {
        unsigned int e = mysql_stmt_errno(st);
        if (err_retryable(e))
            return 1;
        die_stmt(st, what);
    }
    return 0;
}

/* Execute a SELECT and fetch all rows to MYSQL_NO_DATA.
   Returns 0 on success, 1 on a retryable concurrency error; dies otherwise. */
static int exec_select(MYSQL_STMT *st, const char *what)
{
    if (mysql_stmt_execute(st)) {
        unsigned int e = mysql_stmt_errno(st);
        if (err_retryable(e))
            return 1;
        die_stmt(st, what);
    }
    for (;;) {
        int rc = mysql_stmt_fetch(st);
        if (rc == 0 || rc == MYSQL_DATA_TRUNCATED)
            continue; /* row consumed */
        if (rc == MYSQL_NO_DATA)
            return 0;
        /* rc == 1: error */
        {
            unsigned int e = mysql_stmt_errno(st);
            if (err_retryable(e))
                return 1;
            die_stmt(st, what);
        }
    }
}

/* One attempt at the transaction body (the 18 inner statements).
   Values are drawn immediately before each statement executes, so a retry
   naturally redraws everything (the PRNG stream continues).
   Returns 0 on success, 1 if the attempt must be retried; dies otherwise. */
static int txn_body(worker *w)
{
    /* 10 point selects */
    for (int j = 0; j < 10; j++) {
        w->point_id[j] = (unsigned int)(1 + prng_rand(w, table_rows));
        if (exec_select(w->st[ST_POINT0 + j], "point SELECT"))
            return 1;
    }

    /* 4 range selects: simple, sum, order, distinct */
    static const char *range_what[4] = {
        "simple range SELECT", "sum range SELECT",
        "order range SELECT", "distinct range SELECT"
    };
    for (int j = 0; j < 4; j++) {
        unsigned int lo = (unsigned int)(1 + prng_rand(w, table_rows));
        w->range_lo[j] = lo;
        w->range_hi[j] = lo + (unsigned int)range_size - 1;
        if (exec_select(w->st[ST_RANGE_SIMPLE + j], range_what[j]))
            return 1;
    }

    /* index update */
    w->upd_index_id = (unsigned int)(1 + prng_rand(w, table_rows));
    if (exec_dml(w->st[ST_UPD_INDEX], "index UPDATE"))
        return 1;

    /* non-index update */
    w->upd_nonindex_id = (unsigned int)(1 + prng_rand(w, table_rows));
    if (exec_dml(w->st[ST_UPD_NONINDEX], "non-index UPDATE"))
        return 1;

    /* delete + insert (same id; k drawn with it, before the DELETE) */
    {
        unsigned int id = (unsigned int)(1 + prng_rand(w, table_rows));
        unsigned int k = (unsigned int)prng_rand(w, table_rows);
        w->del_id = id;
        if (exec_dml(w->st[ST_DELETE], "DELETE"))
            return 1;
        w->ins_id = id;
        w->ins_k = k;
        if (exec_dml(w->st[ST_INSERT], "INSERT"))
            return 1;
    }

    return 0;
}

/* Run one full transaction until it commits. Retryable errors (1213/1205/1062)
   anywhere in it — including BEGIN/COMMIT — roll back, bump retries, redraw
   and rerun. */
static void run_txn(worker *w)
{
    for (;;) {
        if (mysql_query(w->conn, "BEGIN")) {
            unsigned int e = mysql_errno(w->conn);
            if (!err_retryable(e))
                die_mysql(w->conn, "BEGIN");
            (void)mysql_query(w->conn, "ROLLBACK"); /* ignore result */
            w->retries++;
            continue;
        }
        if (txn_body(w)) {
            (void)mysql_query(w->conn, "ROLLBACK"); /* ignore result */
            w->retries++;
            continue;
        }
        if (mysql_query(w->conn, "COMMIT")) {
            unsigned int e = mysql_errno(w->conn);
            if (!err_retryable(e))
                die_mysql(w->conn, "COMMIT");
            (void)mysql_query(w->conn, "ROLLBACK"); /* ignore result */
            w->retries++;
            continue;
        }
        return;
    }
}

/* ---------------- worker thread -------------------------------------------- */

static void *worker_main(void *arg)
{
    worker *w = (worker *)arg;

    worker_connect(w);
    worker_prepare(w);

    /* Warmup: executed, same PRNG stream, not timed. */
    for (long i = 0; i < warmup_txns; i++)
        run_txn(w);
    /* Reported retries cover only the timed window (all impls agree). */
    w->retries = 0;

    /* Wait for all workers (and main) at the start barrier. */
    pthread_barrier_wait(&start_barrier);

    /* Timed loop: per-transaction latency in integer microseconds, from just
       before the first BEGIN attempt to after the successful COMMIT. */
    for (long i = 0; i < txns_per_thread; i++) {
        int64_t t0 = now_ns();
        run_txn(w);
        w->lat[i] = (now_ns() - t0) / 1000;
    }

    for (int j = 0; j < ST_COUNT; j++)
        mysql_stmt_close(w->st[j]);
    mysql_close(w->conn);
    mysql_thread_end();
    return NULL;
}

/* ---------------- main ----------------------------------------------------- */

int main(void)
{
    /* Connection config */
    cfg_host = env_str("MYSQL_HOST");
    cfg_port = env_int("MYSQL_PORT");
    cfg_user = env_str("MYSQL_USER");
    cfg_password = env_str("MYSQL_PASSWORD");
    cfg_db = env_str("MYSQL_DB");

    /* Workload config */
    table_rows = env_int("BENCH_TABLE_ROWS");
    n_threads = env_int("BENCH_THREADS");
    warmup_txns = env_int("BENCH_WARMUP_TXNS");
    txns_per_thread = env_int("BENCH_TXNS_PER_THREAD");
    range_size = env_int("BENCH_RANGE_SIZE");
    base_seed = env_int("BENCH_SEED");

    if (n_threads <= 0 || txns_per_thread <= 0 || table_rows <= 0 ||
        range_size <= 0 || warmup_txns < 0) {
        fprintf(stderr, "invalid workload configuration\n");
        return 1;
    }

    /* Must be called before any threads touch the client library. */
    if (mysql_library_init(0, NULL, NULL)) {
        fprintf(stderr, "mysql_library_init failed\n");
        return 1;
    }

    worker *workers = calloc((size_t)n_threads, sizeof(worker));
    if (workers == NULL) {
        fprintf(stderr, "failed to allocate %ld worker structs\n", n_threads);
        return 1;
    }
    for (long i = 0; i < n_threads; i++) {
        workers[i].index = (int)i;
        workers[i].prng_state = base_seed + i;
        workers[i].retries = 0;
        workers[i].lat =
            malloc((size_t)txns_per_thread * sizeof(int64_t));
        if (workers[i].lat == NULL) {
            fprintf(stderr, "failed to allocate latency array (%ld txns)\n",
                    txns_per_thread);
            return 1;
        }
    }

    if (pthread_barrier_init(&start_barrier, NULL,
                             (unsigned int)n_threads + 1)) {
        fprintf(stderr, "pthread_barrier_init failed\n");
        return 1;
    }

    for (long i = 0; i < n_threads; i++) {
        if (pthread_create(&workers[i].tid, NULL, worker_main, &workers[i])) {
            fprintf(stderr, "pthread_create failed for worker %ld\n", i);
            return 1;
        }
    }

    /* Workers connect, prepare and warm up, then block on the barrier.
       When the last one arrives, everybody is released: sample t_start. */
    pthread_barrier_wait(&start_barrier);
    int64_t t_start = now_ns();

    for (long i = 0; i < n_threads; i++) {
        if (pthread_join(workers[i].tid, NULL)) {
            fprintf(stderr, "pthread_join failed for worker %ld\n", i);
            return 1;
        }
    }
    int64_t t_end = now_ns();

    pthread_barrier_destroy(&start_barrier);

    const char *driver_version = mysql_get_client_info();

    /* Merge latency arrays, sort, percentiles. */
    long txns = n_threads * txns_per_thread;
    int64_t *lat = malloc((size_t)txns * sizeof(int64_t));
    if (lat == NULL) {
        fprintf(stderr, "failed to allocate merged latency array\n");
        return 1;
    }
    int64_t retries = 0;
    for (long i = 0; i < n_threads; i++) {
        memcpy(lat + i * txns_per_thread, workers[i].lat,
               (size_t)txns_per_thread * sizeof(int64_t));
        retries += workers[i].retries;
        free(workers[i].lat);
    }
    qsort(lat, (size_t)txns, sizeof(int64_t), cmp_i64);
    int64_t p50 = percentile(lat, txns, 50);
    int64_t p95 = percentile(lat, txns, 95);
    int64_t p99 = percentile(lat, txns, 99);
    int64_t max_us = lat[txns - 1];

    double elapsed_s = (double)(t_end - t_start) / 1e9;
    double tps = (double)txns / elapsed_s;
    double qps = tps * 20.0; /* 18 statements + BEGIN + COMMIT */

    char client_host[256];
    if (gethostname(client_host, sizeof(client_host)) != 0)
        snprintf(client_host, sizeof(client_host), "unknown");
    client_host[sizeof(client_host) - 1] = '\0';

    printf("{\"lang\":\"c\",\"driver\":\"libmysqlclient\","
           "\"driver_version\":\"%s\",\"runtime_version\":\"%s\","
           "\"prepared\":true,\"client_host\":\"%s\",\"db_host\":\"%s\","
           "\"threads\":%ld,\"txns\":%ld,\"retries\":%" PRId64
           ",\"errors\":0,\"elapsed_s\":%.3f,\"tps\":%.1f,\"qps\":%.1f,"
           "\"p50_us\":%" PRId64 ",\"p95_us\":%" PRId64
           ",\"p99_us\":%" PRId64 ",\"max_us\":%" PRId64 "}\n",
           driver_version, RUNTIME_VERSION, client_host, cfg_host,
           n_threads, txns, retries, elapsed_s, tps, qps,
           p50, p95, p99, max_us);

    free(lat);
    free(workers);
    mysql_library_end();
    return 0;
}
