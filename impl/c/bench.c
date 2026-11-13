/*
 * mysql-lang-bench — C implementation (v3, four micro-workloads).
 *
 * Follows SPEC.md v3 exactly: one of four workloads selected by BENCH_WORKLOAD
 * (1..4), run by BENCH_THREADS concurrent pthread workers. Each worker owns its
 * own MYSQL* connection (TCP, TLS disabled, autocommit ON), its own server-side
 * prepared statement (mysql_stmt_*, so prepared=true), its own 31-bit LCG PRNG
 * stream seeded BENCH_SEED + index, and its own per-operation latency array
 * (integer microseconds). Workers connect and prepare, then wait at a start
 * barrier; main releases the barrier, samples a monotonic clock, joins all
 * workers, samples the clock again, and prints one JSON result line on stdout.
 *
 *   1 int-insert : BENCH_INT_ROWS rows via one prepared multi-row INSERT of
 *                  BENCH_INT_BATCH rows, executed BENCH_INT_ROWS/BENCH_INT_BATCH
 *                  times; one latency sample per INSERT.
 *   2 int-read   : SELECT val FROM bench_int LIMIT BENCH_INT_ROWS, sum val into
 *                  a 64-bit sink; one latency sample for the whole scan.
 *   3 blob-insert: BENCH_BLOB_COUNT blobs of BENCH_BLOB_SIZE PRNG bytes, MD5
 *                  each (hex digest into a sink), INSERT INTO bench_blob(id,data)
 *                  with id = index*BENCH_BLOB_COUNT + k; one sample per insert.
 *   4 blob-read  : read own BENCH_BLOB_COUNT blobs by id, MD5 each; one sample
 *                  per read.
 *
 * Intermediate progress (SPEC "Intermediate progress"): when BENCH_PROGRESS_FILE
 * is set, each worker publishes its completed-op count to a per-worker slot it
 * alone writes (volatile long, single-writer). A monitor thread sums the slots
 * every BENCH_PROGRESS_SEC seconds and atomically overwrites the progress file
 * (write FILE.tmp, rename over FILE). The file is removed on success.
 *
 * Any SQL/connection error -> stderr + exit 1. No retries (these workloads have
 * no concurrency conflicts).
 */

#define _GNU_SOURCE

#include <mysql.h>

#include <openssl/md5.h>

#include <inttypes.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

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

/* ---------------- env parsing -------------------------------------------- */

static const char *env_str(const char *name)
{
    const char *v = getenv(name);
    if (v == NULL || v[0] == '\0') {
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

static long env_int_opt(const char *name, long def)
{
    const char *v = getenv(name);
    if (v == NULL || v[0] == '\0')
        return def;
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

/* p(x) = arr[ceil(x/100 * n) - 1], arr sorted ascending, 0-based index. */
static int64_t percentile(const int64_t *arr, long n, int x)
{
    long idx = (long)(((int64_t)x * n + 99) / 100) - 1;
    if (idx < 0)
        idx = 0;
    if (idx >= n)
        idx = n - 1;
    return arr[idx];
}

/* ---------------- MD5 helper ---------------------------------------------- */

/* Compute MD5 over buf, format the 32-char hex digest, and fold its bytes into
 * a sink value so the app-side work cannot be optimized away. Returns the sink
 * contribution. */
static uint64_t md5_hex_sink(const unsigned char *buf, size_t len)
{
    unsigned char dg[MD5_DIGEST_LENGTH];
    MD5(buf, len, dg);
    static const char hx[] = "0123456789abcdef";
    uint64_t s = 0;
    for (int i = 0; i < MD5_DIGEST_LENGTH; i++) {
        char hi = hx[dg[i] >> 4];
        char lo = hx[dg[i] & 0x0F];
        s += (unsigned char)hi + (unsigned char)lo;
    }
    return s;
}

/* ---------------- shared, read-only workload config ----------------------- */

static const char *cfg_host, *cfg_user, *cfg_password, *cfg_db;
static long cfg_port;
static long n_threads, base_seed;
static long int_rows, int_batch, blob_count, blob_size;
static int workload;
static const char *workload_name;
static const char *unit;

static pthread_barrier_t start_barrier;

/* Progress checkpointing. */
static const char *progress_file;  /* NULL/empty => disabled */
static long progress_sec;
static volatile long *progress;    /* per-worker slots, or NULL */
static int64_t t_start_ns;         /* set by main after barrier release */
static volatile int monitor_stop;

/* Black-box sink so MD5/sum work is not dead-code-eliminated. */
static volatile uint64_t g_sink;

/* ---------------- per-worker state ---------------------------------------- */

typedef struct worker {
    pthread_t tid;
    int index;

    int64_t prng_state;

    MYSQL *conn;
    MYSQL_STMT *st;

    MYSQL_BIND *pbind;      /* parameter binds (dynamic count per workload) */
    MYSQL_BIND rbind[1];    /* result bind */

    int32_t *ivals;         /* WL1: BENCH_INT_BATCH bound int values */
    int32_t ival;           /* WL2: fetched val */
    unsigned char *blob;    /* WL3/4: BENCH_BLOB_SIZE byte buffer */
    long long blob_id;      /* WL3/4: bound/looked-up id (BIGINT) */
    unsigned long blob_len; /* WL3: param length; WL4: fetched length */

    int64_t *lat;
    long lat_cap;
    long lat_count;

    uint64_t sink;          /* per-worker accumulation of consumed values */

    volatile long *slot;    /* progress slot (completed ops) or NULL */
} worker;

/* ---------------- PRNG: 31-bit LCG, identical in every language ----------- */

static int64_t prng_next(worker *w)
{
    /* (state * 1103515245 + 12345) mod 2^31; product fits in int64. */
    w->prng_state = (w->prng_state * 1103515245 + 12345) & 0x7fffffffLL;
    return w->prng_state;
}

/* ---------------- connection / statement setup ---------------------------- */

static MYSQL_STMT *prepare_stmt(MYSQL *conn, const char *sql)
{
    MYSQL_STMT *stmt = mysql_stmt_init(conn);
    if (stmt == NULL)
        die_mysql(conn, "mysql_stmt_init");
    if (mysql_stmt_prepare(stmt, sql, (unsigned long)strlen(sql)))
        die_stmt(stmt, "mysql_stmt_prepare");
    return stmt;
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

/* ---------------- per-workload prepare ------------------------------------ */

static void prepare_wl1(worker *w)
{
    /* One prepared multi-row INSERT of BENCH_INT_BATCH rows. */
    size_t cap = 64 + (size_t)int_batch * 5;
    char *sql = malloc(cap);
    if (sql == NULL) {
        fprintf(stderr, "out of memory building WL1 SQL\n");
        exit(1);
    }
    int off = snprintf(sql, cap, "INSERT INTO bench_int (val) VALUES ");
    for (long j = 0; j < int_batch; j++)
        off += snprintf(sql + off, cap - (size_t)off, "%s(?)", j ? "," : "");
    w->st = prepare_stmt(w->conn, sql);
    free(sql);

    w->ivals = malloc((size_t)int_batch * sizeof(int32_t));
    w->pbind = calloc((size_t)int_batch, sizeof(MYSQL_BIND));
    if (w->ivals == NULL || w->pbind == NULL) {
        fprintf(stderr, "out of memory allocating WL1 buffers\n");
        exit(1);
    }
    for (long j = 0; j < int_batch; j++) {
        w->pbind[j].buffer_type = MYSQL_TYPE_LONG; /* signed 32-bit */
        w->pbind[j].buffer = &w->ivals[j];
        w->pbind[j].is_unsigned = 0;
    }
    if (mysql_stmt_bind_param(w->st, w->pbind))
        die_stmt(w->st, "mysql_stmt_bind_param(wl1)");
}

static void prepare_wl2(worker *w)
{
    char sql[128];
    snprintf(sql, sizeof(sql), "SELECT val FROM bench_int LIMIT %ld", int_rows);
    w->st = prepare_stmt(w->conn, sql);

    memset(w->rbind, 0, sizeof(w->rbind));
    w->rbind[0].buffer_type = MYSQL_TYPE_LONG; /* signed INT */
    w->rbind[0].buffer = &w->ival;
    w->rbind[0].is_unsigned = 0;
    if (mysql_stmt_bind_result(w->st, w->rbind))
        die_stmt(w->st, "mysql_stmt_bind_result(wl2)");
}

static void prepare_wl3(worker *w)
{
    w->blob = malloc((size_t)blob_size);
    w->pbind = calloc(2, sizeof(MYSQL_BIND));
    if (w->blob == NULL || w->pbind == NULL) {
        fprintf(stderr, "out of memory allocating WL3 buffers\n");
        exit(1);
    }
    w->blob_len = (unsigned long)blob_size;

    w->st = prepare_stmt(w->conn,
                         "INSERT INTO bench_blob (id, data) VALUES (?, ?)");
    w->pbind[0].buffer_type = MYSQL_TYPE_LONGLONG; /* id BIGINT */
    w->pbind[0].buffer = &w->blob_id;
    w->pbind[0].is_unsigned = 0;
    w->pbind[1].buffer_type = MYSQL_TYPE_LONG_BLOB; /* data LONGBLOB */
    w->pbind[1].buffer = w->blob;
    w->pbind[1].buffer_length = (unsigned long)blob_size;
    w->pbind[1].length = &w->blob_len;
    if (mysql_stmt_bind_param(w->st, w->pbind))
        die_stmt(w->st, "mysql_stmt_bind_param(wl3)");
}

static void prepare_wl4(worker *w)
{
    w->blob = malloc((size_t)blob_size);
    w->pbind = calloc(1, sizeof(MYSQL_BIND));
    if (w->blob == NULL || w->pbind == NULL) {
        fprintf(stderr, "out of memory allocating WL4 buffers\n");
        exit(1);
    }

    w->st = prepare_stmt(w->conn, "SELECT data FROM bench_blob WHERE id = ?");
    w->pbind[0].buffer_type = MYSQL_TYPE_LONGLONG; /* id BIGINT */
    w->pbind[0].buffer = &w->blob_id;
    w->pbind[0].is_unsigned = 0;
    if (mysql_stmt_bind_param(w->st, w->pbind))
        die_stmt(w->st, "mysql_stmt_bind_param(wl4)");

    memset(w->rbind, 0, sizeof(w->rbind));
    w->rbind[0].buffer_type = MYSQL_TYPE_LONG_BLOB; /* data LONGBLOB */
    w->rbind[0].buffer = w->blob;
    w->rbind[0].buffer_length = (unsigned long)blob_size;
    w->rbind[0].length = &w->blob_len;
    if (mysql_stmt_bind_result(w->st, w->rbind))
        die_stmt(w->st, "mysql_stmt_bind_result(wl4)");
}

/* ---------------- latency recording --------------------------------------- */

static void record_lat(worker *w, int64_t us)
{
    if (w->lat_count >= w->lat_cap) {
        /* Capacities are sized exactly per workload; this should never fire. */
        fprintf(stderr, "latency array overflow (cap %ld)\n", w->lat_cap);
        exit(1);
    }
    w->lat[w->lat_count++] = us;
}

/* ---------------- per-workload run ---------------------------------------- */

static void run_wl1(worker *w)
{
    long execs = int_rows / int_batch;
    long done = 0;
    for (long e = 0; e < execs; e++) {
        for (long j = 0; j < int_batch; j++)
            w->ivals[j] = (int32_t)prng_next(w);
        int64_t t0 = now_ns();
        if (mysql_stmt_execute(w->st))
            die_stmt(w->st, "INSERT (wl1)");
        record_lat(w, (now_ns() - t0) / 1000);
        done += int_batch;
        if (w->slot)
            *w->slot = done; /* rows completed; published every batch */
    }
}

static void run_wl2(worker *w)
{
    int64_t t0 = now_ns();
    if (mysql_stmt_execute(w->st))
        die_stmt(w->st, "SELECT (wl2)");
    int64_t sum = 0;
    long rows = 0;
    for (;;) {
        int rc = mysql_stmt_fetch(w->st);
        if (rc == MYSQL_NO_DATA)
            break;
        if (rc == 1)
            die_stmt(w->st, "mysql_stmt_fetch (wl2)");
        /* rc == 0 or MYSQL_DATA_TRUNCATED: row consumed */
        sum += w->ival;
        rows++;
        if (w->slot && (rows & 0xFFFF) == 0)
            *w->slot = rows;
    }
    record_lat(w, (now_ns() - t0) / 1000); /* one sample for the whole scan */
    w->sink += (uint64_t)sum;
    if (w->slot)
        *w->slot = rows;
}

static void run_wl3(worker *w)
{
    /* Generate the BENCH_BLOB_SIZE buffer ONCE per worker with a bulk LCG fill
       (8 bytes written per LCG step in compiled code — NOT a per-byte loop, so
       this measures MD5/DB throughput, not loop speed). Before each blob insert
       the first 8 bytes are overwritten with the blob id, so every blob's
       content and MD5 differ. Per SPEC the content need not match across
       languages. */
    long nchunks = blob_size / 8;
    uint64_t *p64 = (uint64_t *)w->blob;
    for (long c = 0; c < nchunks; c++)
        p64[c] = (uint64_t)prng_next(w) * 1103515245ULL + 12345ULL;
    for (long j = nchunks * 8; j < blob_size; j++)
        w->blob[j] = (unsigned char)(prng_next(w) & 0xFF);

    w->blob_len = (unsigned long)blob_size;
    for (long k = 0; k < blob_count; k++) {
        w->blob_id = (long long)w->index * blob_count + k;
        if (blob_size >= 8)
            memcpy(w->blob, &w->blob_id, 8); /* vary content per blob */
        w->sink += md5_hex_sink(w->blob, (size_t)blob_size);
        int64_t t0 = now_ns();
        if (mysql_stmt_execute(w->st))
            die_stmt(w->st, "INSERT (wl3)");
        record_lat(w, (now_ns() - t0) / 1000); /* per blob insert */
        if (w->slot)
            *w->slot = k + 1;
    }
}

static void run_wl4(worker *w)
{
    for (long k = 0; k < blob_count; k++) {
        w->blob_id = (long long)w->index * blob_count + k;
        w->blob_len = 0;
        int64_t t0 = now_ns();
        if (mysql_stmt_execute(w->st))
            die_stmt(w->st, "SELECT (wl4)");
        int rc = mysql_stmt_fetch(w->st);
        if (rc == 1)
            die_stmt(w->st, "mysql_stmt_fetch (wl4)");
        if (rc == MYSQL_NO_DATA) {
            fprintf(stderr, "blob id %lld not found\n", w->blob_id);
            exit(1);
        }
        unsigned long got = w->blob_len; /* full content length */
        /* Drain the (single-row) result so the statement can re-execute. */
        while ((rc = mysql_stmt_fetch(w->st)) == 0 || rc == MYSQL_DATA_TRUNCATED)
            ;
        if (rc == 1)
            die_stmt(w->st, "mysql_stmt_fetch drain (wl4)");
        record_lat(w, (now_ns() - t0) / 1000); /* per blob read */
        w->sink += md5_hex_sink(w->blob, (size_t)got);
        if (w->slot)
            *w->slot = k + 1;
    }
}

/* ---------------- worker thread ------------------------------------------- */

static void *worker_main(void *arg)
{
    worker *w = (worker *)arg;

    mysql_thread_init();
    worker_connect(w);

    switch (workload) {
    case 1: prepare_wl1(w); break;
    case 2: prepare_wl2(w); break;
    case 3: prepare_wl3(w); break;
    case 4: prepare_wl4(w); break;
    }

    pthread_barrier_wait(&start_barrier);

    switch (workload) {
    case 1: run_wl1(w); break;
    case 2: run_wl2(w); break;
    case 3: run_wl3(w); break;
    case 4: run_wl4(w); break;
    }

    mysql_stmt_close(w->st);
    mysql_close(w->conn);
    mysql_thread_end();

    free(w->pbind);
    free(w->ivals);
    free(w->blob);
    return NULL;
}

/* ---------------- progress monitor ---------------------------------------- */

static void write_progress(int64_t ops, double elapsed)
{
    double throughput = elapsed > 0 ? (double)ops / elapsed : 0.0;

    size_t tcap = strlen(progress_file) + 5;
    char *tmp = malloc(tcap);
    if (tmp == NULL)
        return; /* best-effort */
    snprintf(tmp, tcap, "%s.tmp", progress_file);

    FILE *f = fopen(tmp, "w");
    if (f == NULL) {
        free(tmp);
        return;
    }
    fprintf(f,
            "{\"workload\":%d,\"workload_name\":\"%s\",\"unit\":\"%s\","
            "\"threads\":%ld,\"ops\":%" PRId64 ",\"elapsed_s\":%.3f,"
            "\"throughput\":%.1f,\"partial\":true}\n",
            workload, workload_name, unit, n_threads, ops, elapsed, throughput);
    if (fclose(f) != 0) {
        free(tmp);
        return;
    }
    (void)rename(tmp, progress_file); /* atomic overwrite; best-effort */
    free(tmp);
}

static void *monitor_main(void *arg)
{
    worker *workers = (worker *)arg;
    for (;;) {
        /* Sleep up to progress_sec, waking promptly on stop. */
        for (long ms = 0; ms < progress_sec * 1000 && !monitor_stop; ms += 100) {
            struct timespec req = { .tv_sec = 0, .tv_nsec = 100 * 1000000L };
            nanosleep(&req, NULL);
        }
        if (monitor_stop)
            break;
        int64_t ops = 0;
        for (long i = 0; i < n_threads; i++)
            ops += progress[i];
        double elapsed = (double)(now_ns() - t_start_ns) / 1e9;
        write_progress(ops, elapsed);
    }
    (void)workers;
    return NULL;
}

/* ---------------- main ---------------------------------------------------- */

int main(void)
{
    cfg_host = env_str("MYSQL_HOST");
    cfg_port = env_int("MYSQL_PORT");
    cfg_user = env_str("MYSQL_USER");
    cfg_password = env_str("MYSQL_PASSWORD");
    cfg_db = env_str("MYSQL_DB");

    workload = (int)env_int("BENCH_WORKLOAD");
    n_threads = env_int("BENCH_THREADS");
    base_seed = env_int("BENCH_SEED");
    int_rows = env_int_opt("BENCH_INT_ROWS", 1000000);
    int_batch = env_int_opt("BENCH_INT_BATCH", 10);
    blob_count = env_int_opt("BENCH_BLOB_COUNT", 100);
    blob_size = env_int_opt("BENCH_BLOB_SIZE", 1048576);

    const char *pf = getenv("BENCH_PROGRESS_FILE");
    progress_file = (pf != NULL && pf[0] != '\0') ? pf : NULL;
    progress_sec = env_int_opt("BENCH_PROGRESS_SEC", 5);
    if (progress_sec <= 0)
        progress_sec = 5;

    switch (workload) {
    case 1: workload_name = "int-insert"; unit = "rows/s"; break;
    case 2: workload_name = "int-read"; unit = "rows/s"; break;
    case 3: workload_name = "blob-insert"; unit = "blobs/s"; break;
    case 4: workload_name = "blob-read"; unit = "blobs/s"; break;
    default:
        fprintf(stderr, "BENCH_WORKLOAD must be 1..4, got %d\n", workload);
        return 1;
    }

    if (n_threads <= 0 || int_rows <= 0 || int_batch <= 0 ||
        blob_count <= 0 || blob_size <= 0) {
        fprintf(stderr, "invalid workload configuration\n");
        return 1;
    }

    if (mysql_library_init(0, NULL, NULL)) {
        fprintf(stderr, "mysql_library_init failed\n");
        return 1;
    }

    worker *workers = calloc((size_t)n_threads, sizeof(worker));
    if (workers == NULL) {
        fprintf(stderr, "failed to allocate %ld worker structs\n", n_threads);
        return 1;
    }

    /* Latency samples per worker: WL1 one per INSERT, WL2 one per worker,
       WL3/4 one per blob. */
    long lat_cap;
    switch (workload) {
    case 1: lat_cap = int_rows / int_batch; break;
    case 2: lat_cap = 1; break;
    default: lat_cap = blob_count; break;
    }
    if (lat_cap <= 0) {
        fprintf(stderr, "invalid latency capacity (check INT_ROWS/INT_BATCH)\n");
        return 1;
    }

    if (progress_file) {
        progress = calloc((size_t)n_threads, sizeof(long));
        if (progress == NULL) {
            fprintf(stderr, "failed to allocate progress slots\n");
            return 1;
        }
    }

    for (long i = 0; i < n_threads; i++) {
        workers[i].index = (int)i;
        workers[i].prng_state = base_seed + i;
        workers[i].lat_cap = lat_cap;
        workers[i].lat_count = 0;
        workers[i].sink = 0;
        workers[i].slot = progress ? &progress[i] : NULL;
        workers[i].lat = malloc((size_t)lat_cap * sizeof(int64_t));
        if (workers[i].lat == NULL) {
            fprintf(stderr, "failed to allocate latency array (%ld entries)\n",
                    lat_cap);
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

    /* Workers connect and prepare, then block on the barrier. When the last
       arrives, everybody is released: sample t_start. */
    pthread_barrier_wait(&start_barrier);
    int64_t t_start = now_ns();
    t_start_ns = t_start;

    pthread_t monitor_tid = 0;
    bool have_monitor = false;
    if (progress_file) {
        if (pthread_create(&monitor_tid, NULL, monitor_main, workers) == 0)
            have_monitor = true;
        else
            fprintf(stderr, "warning: progress monitor thread not started\n");
    }

    for (long i = 0; i < n_threads; i++) {
        if (pthread_join(workers[i].tid, NULL)) {
            fprintf(stderr, "pthread_join failed for worker %ld\n", i);
            return 1;
        }
    }
    int64_t t_end = now_ns();

    if (have_monitor) {
        monitor_stop = 1;
        pthread_join(monitor_tid, NULL);
    }

    pthread_barrier_destroy(&start_barrier);

    const char *driver_version = mysql_get_client_info();

    /* Merge latency arrays, sort, percentiles. */
    long n_lat = 0;
    for (long i = 0; i < n_threads; i++)
        n_lat += workers[i].lat_count;
    if (n_lat <= 0) {
        fprintf(stderr, "no latency samples recorded\n");
        return 1;
    }
    int64_t *lat = malloc((size_t)n_lat * sizeof(int64_t));
    if (lat == NULL) {
        fprintf(stderr, "failed to allocate merged latency array\n");
        return 1;
    }
    long merged = 0;
    uint64_t sink_total = 0;
    for (long i = 0; i < n_threads; i++) {
        memcpy(lat + merged, workers[i].lat,
               (size_t)workers[i].lat_count * sizeof(int64_t));
        merged += workers[i].lat_count;
        sink_total += workers[i].sink;
        free(workers[i].lat);
    }
    g_sink = sink_total; /* consume so app-side work is not eliminated */

    qsort(lat, (size_t)n_lat, sizeof(int64_t), cmp_i64);
    int64_t p50 = percentile(lat, n_lat, 50);
    int64_t p95 = percentile(lat, n_lat, 95);
    int64_t p99 = percentile(lat, n_lat, 99);
    int64_t max_us = lat[n_lat - 1];

    double elapsed_s = (double)(t_end - t_start) / 1e9;

    int64_t ops;
    double mb_per_s = 0.0;
    bool has_mb = false;
    if (workload == 1 || workload == 2) {
        ops = (int64_t)n_threads * int_rows;
    } else {
        ops = (int64_t)n_threads * blob_count;
        mb_per_s = (double)ops * (double)blob_size / 1e6 / elapsed_s;
        has_mb = true;
    }
    double throughput = elapsed_s > 0 ? (double)ops / elapsed_s : 0.0;

    char client_host[256];
    if (gethostname(client_host, sizeof(client_host)) != 0)
        snprintf(client_host, sizeof(client_host), "unknown");
    client_host[sizeof(client_host) - 1] = '\0';

    printf("{\"lang\":\"c\",\"workload\":%d,\"workload_name\":\"%s\","
           "\"unit\":\"%s\",\"driver\":\"libmysqlclient\","
           "\"driver_version\":\"%s\",\"runtime_version\":\"%s\","
           "\"prepared\":true,\"client_host\":\"%s\",\"db_host\":\"%s\","
           "\"threads\":%ld,\"ops\":%" PRId64 ",\"errors\":0,"
           "\"elapsed_s\":%.3f,\"throughput\":%.1f,",
           workload, workload_name, unit, driver_version, RUNTIME_VERSION,
           client_host, cfg_host, n_threads, ops, elapsed_s, throughput);
    if (has_mb)
        printf("\"mb_per_s\":%.1f,", mb_per_s);
    else
        printf("\"mb_per_s\":0,");
    printf("\"p50_us\":%" PRId64 ",\"p95_us\":%" PRId64 ",\"p99_us\":%" PRId64
           ",\"max_us\":%" PRId64 ",\"client_cpu_s\":0,\"server_cpu_s\":0}\n",
           p50, p95, p99, max_us);

    /* Final JSON is on stdout; the partial progress file is now superseded. */
    if (progress_file) {
        size_t tcap = strlen(progress_file) + 5;
        char *tmp = malloc(tcap);
        if (tmp != NULL) {
            snprintf(tmp, tcap, "%s.tmp", progress_file);
            (void)unlink(tmp);
            free(tmp);
        }
        (void)unlink(progress_file);
    }

    free(lat);
    free(workers);
    mysql_library_end();
    return 0;
}
