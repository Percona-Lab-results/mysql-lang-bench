-- mysql-lang-bench — Lua implementation (v2: sysbench oltp_read_write-style).
-- LuaJIT FFI over libmysqlclient, TEXT protocol: mysql_stmt_* is deliberately
-- not used (the MYSQL_BIND struct layout is version-fragile in FFI), so all
-- values are interpolated into the SQL string — integers and fixed ASCII
-- constants only, no escaping needed — and "prepared" is reported as false
-- (Python-style). See SPEC.md.
--
-- Workers are forked processes (Lua has no threads), PHP-style:
--   - two pipes per worker (ready-pipe, start-pipe), created just before fork;
--   - the child connects to MySQL (connections are created only AFTER fork;
--     the parent never connects), runs BENCH_WARMUP_TXNS untimed transactions,
--     writes one ready byte, then blocks reading one start byte (the barrier);
--   - the parent reads one ready byte per child (EOF = child died -> abort),
--     samples t_start, writes the start byte to every child, waitpid()s every
--     child, samples t_end;
--   - each child writes its latencies (one integer microsecond value per line)
--     plus a final "retries=<n>" line to /tmp/bench-lua-<pid>.result and
--     _exit(0)s; the parent merges those files and prints the single JSON
--     result line.

local ffi = require("ffi")
local bit = require("bit")
local jit = require("jit")

---------------------------------------------------------------------------
-- FFI declarations
---------------------------------------------------------------------------

ffi.cdef[[
/* ---- libc ---- */
struct timespec { long tv_sec; long tv_nsec; };
int clock_gettime(int clk_id, struct timespec *tp);
int fork(void);
int pipe(int pipefd[2]);
long read(int fd, char *buf, unsigned long count);
long write(int fd, const char *buf, unsigned long count);
int close(int fd);
int waitpid(int pid, int *status, int options);
int getpid(void);
void _exit(int status);
int gethostname(char *name, unsigned long len);
typedef void (*sighandler_t)(int);
sighandler_t signal(int signum, sighandler_t handler);

/* ---- libmysqlclient: stable pointer-based C API only, opaque structs ---- */
typedef struct MYSQL MYSQL;
typedef struct MYSQL_RES MYSQL_RES;
MYSQL *mysql_init(MYSQL *mysql);
int mysql_options(MYSQL *mysql, int option, const void *arg);
MYSQL *mysql_real_connect(MYSQL *mysql, const char *host, const char *user,
                          const char *passwd, const char *db,
                          unsigned int port, const char *unix_socket,
                          unsigned long client_flag);
int mysql_real_query(MYSQL *mysql, const char *stmt_str, unsigned long length);
MYSQL_RES *mysql_store_result(MYSQL *mysql);
char **mysql_fetch_row(MYSQL_RES *result);
unsigned int mysql_num_fields(MYSQL_RES *result);
void mysql_free_result(MYSQL_RES *result);
unsigned int mysql_errno(MYSQL *mysql);
const char *mysql_error(MYSQL *mysql);
void mysql_close(MYSQL *mysql);
const char *mysql_get_client_info(void);
/* mysql_library_init/mysql_library_end are macros in mysql.h; these are the
   real exported symbols they expand to. */
int mysql_server_init(int argc, char **argv, char **groups);
void mysql_server_end(void);
]]

local C = ffi.C

local function load_mysql()
    local candidates = { "mysqlclient", "libmysqlclient.so.24",
                         "libmysqlclient.so.21", "libmysqlclient.so" }
    for _, name in ipairs(candidates) do
        local ok, lib = pcall(ffi.load, name)
        if ok then return lib end
    end
    io.stderr:write("failed to load libmysqlclient (tried mysqlclient, " ..
                    "libmysqlclient.so.24, .so.21, .so): " ..
                    "install libmysqlclient-dev / libmysqlclient21+\n")
    os.exit(1)
end
local mysql = load_mysql()

---------------------------------------------------------------------------
-- Helpers (parent context; children use wfail/_exit)
---------------------------------------------------------------------------

local function fail(msg)
    io.stderr:write(msg .. "\n")
    os.exit(1)
end

local function env_str(name)
    local v = os.getenv(name)
    if v == nil or v == "" then
        fail("missing required environment variable: " .. name)
    end
    return v
end

local function env_int(name)
    local v = env_str(name)
    local n = tonumber(v)
    if n == nil or n ~= math.floor(n) then
        fail("environment variable " .. name .. " is not an integer: '" .. v .. "'")
    end
    return n
end

local function env_int_opt(name, default)
    local v = os.getenv(name)
    if v == nil or v == "" then return default end
    local n = tonumber(v)
    if n == nil or n ~= math.floor(n) then
        fail("environment variable " .. name .. " is not an integer: '" .. v .. "'")
    end
    return n
end

---------------------------------------------------------------------------
-- Configuration (environment only; no CLI arguments)
---------------------------------------------------------------------------

local password = os.getenv("MYSQL_PASSWORD") -- may be empty, must be set
if password == nil then
    fail("missing required environment variable: MYSQL_PASSWORD")
end

local cfg = {
    host            = env_str("MYSQL_HOST"),
    port            = env_int("MYSQL_PORT"),
    user            = env_str("MYSQL_USER"),
    password        = password,
    db              = env_str("MYSQL_DB"),
    table_rows      = env_int("BENCH_TABLE_ROWS"),
    threads         = env_int("BENCH_THREADS"),
    warmup_txns     = env_int("BENCH_WARMUP_TXNS"),
    txns_per_thread = env_int("BENCH_TXNS_PER_THREAD"),
    range_size      = env_int("BENCH_RANGE_SIZE"),
    seed            = env_int("BENCH_SEED"),
    duration_s      = env_int_opt("BENCH_DURATION_S", 0), -- 0 = events mode
}

if cfg.threads < 1 or cfg.txns_per_thread < 1 or cfg.table_rows < 1
    or cfg.range_size < 1 or cfg.warmup_txns < 0 or cfg.seed < 0
    or cfg.duration_s < 0 then
    fail("invalid workload configuration")
end

---------------------------------------------------------------------------
-- Timing: monotonic clock, nanoseconds as int64 cdata
---------------------------------------------------------------------------

local CLOCK_MONOTONIC = 1 -- Linux
local ts = ffi.new("struct timespec")
local function now_ns()
    C.clock_gettime(CLOCK_MONOTONIC, ts)
    -- tv_sec/tv_nsec are 64-bit (long): all math stays in int64 cdata
    return ts.tv_sec * 1000000000LL + ts.tv_nsec
end

---------------------------------------------------------------------------
-- Workload constants
---------------------------------------------------------------------------

local C_VAL = string.rep("abcdefghij", 12)  -- 120 chars
local PAD_VAL = string.rep("0123456789", 6) -- 60 chars

local function result_file_path(pid)
    return "/tmp/bench-lua-" .. pid .. ".result"
end

---------------------------------------------------------------------------
-- Worker body: runs in the forked child only; never returns (_exit).
---------------------------------------------------------------------------

local function worker_main(worker_idx, ready_w, start_r)
    local fmt = string.format

    local function wfail(msg)
        io.stderr:write("lua worker " .. worker_idx .. ": " .. msg .. "\n")
        C._exit(1)
    end

    -- PRNG: 31-bit LCG, one independent stream per worker, seeded with
    -- BENCH_SEED + worker_idx. All LCG math stays in uint64 cdata: plain Lua
    -- numbers are doubles (53-bit mantissa) and state*1103515245 would lose
    -- bits. Results are < 2^31, exact as doubles after tonumber().
    local state = 0ULL + (cfg.seed + worker_idx)
    local function lcg_rand(n)
        state = (state * 1103515245ULL + 12345ULL) % 2147483648ULL
        return tonumber(state % (0ULL + n))
    end

    if mysql.mysql_server_init(0, nil, nil) ~= 0 then
        wfail("mysql_server_init failed")
    end

    -- Connection (child only, after fork): TCP, autocommit ON (default),
    -- explicit BEGIN/COMMIT as plain queries.
    local conn = mysql.mysql_init(nil)
    if conn == nil then
        wfail("mysql_init failed (out of memory)")
    end

    -- Option constants hardcoded from this host's MySQL 8.4 headers
    -- (enum mysql_option / mysql_protocol_type / mysql_ssl_mode).
    local proto = ffi.new("unsigned int[1]", 1)    -- MYSQL_PROTOCOL_TCP
    mysql.mysql_options(conn, 9, proto)            -- MYSQL_OPT_PROTOCOL
    -- Plaintext everywhere: the libmysqlclient default SSL_MODE_PREFERRED
    -- would put this driver on TLS while others run plaintext.
    local ssl_mode = ffi.new("unsigned int[1]", 1) -- SSL_MODE_DISABLED
    mysql.mysql_options(conn, 35, ssl_mode)        -- MYSQL_OPT_SSL_MODE
    -- caching_sha2_password full auth over plaintext needs the server RSA key.
    local get_pk = ffi.new("bool[1]", true)
    mysql.mysql_options(conn, 36, get_pk)          -- MYSQL_OPT_GET_SERVER_PUBLIC_KEY

    if mysql.mysql_real_connect(conn, cfg.host, cfg.user, cfg.password,
                                cfg.db, cfg.port, nil, 0) == nil then
        wfail(fmt("mysql_real_connect failed: (%d) %s",
                  tonumber(mysql.mysql_errno(conn)),
                  ffi.string(mysql.mysql_error(conn))))
    end

    -- Retryable concurrency errors per SPEC: deadlock, lock wait timeout,
    -- duplicate key on the insert.
    local function retryable(e)
        return e == 1213 or e == 1205 or e == 1062
    end

    local function die_sql(what)
        wfail(fmt("%s failed: (%d) %s", what,
                  tonumber(mysql.mysql_errno(conn)),
                  ffi.string(mysql.mysql_error(conn))))
    end

    -- DML / BEGIN / COMMIT over the text protocol: no result set expected.
    -- Returns true on success, false on a retryable error; dies otherwise.
    local function exec_dml(sql)
        if mysql.mysql_real_query(conn, sql, #sql) ~= 0 then
            if retryable(tonumber(mysql.mysql_errno(conn))) then return false end
            die_sql(sql)
        end
        return true
    end

    -- SELECT: execute, store the result, fetch ALL rows, free the result.
    -- The result is always freed before returning, so a retry never has a
    -- pending result left when ROLLBACK runs.
    local function exec_select(sql)
        if mysql.mysql_real_query(conn, sql, #sql) ~= 0 then
            if retryable(tonumber(mysql.mysql_errno(conn))) then return false end
            die_sql(sql)
        end
        local res = mysql.mysql_store_result(conn)
        if res == nil then
            local e = tonumber(mysql.mysql_errno(conn))
            if e == 0 then return true end -- no result set: fine
            if retryable(e) then return false end
            die_sql(sql)
        end
        while true do
            local row = mysql.mysql_fetch_row(res)
            if row == nil then break end -- all rows must be consumed (SPEC)
        end
        mysql.mysql_free_result(res)
        return true
    end

    local N = cfg.table_rows
    local R = cfg.range_size

    -- One attempt at the sysbench oltp_read_write transaction. Values are
    -- drawn immediately before executing each statement (draw order =
    -- execution order); a retry redraws everything (the PRNG stream
    -- continues). Returns true if committed, false if it must be retried.
    local function txn_once()
        if not exec_dml("BEGIN") then return false end
        for _ = 1, 10 do -- 10 x point select
            if not exec_select(fmt("SELECT c FROM sbtest WHERE id = %d",
                                   1 + lcg_rand(N))) then return false end
        end
        local lo = 1 + lcg_rand(N) -- simple range
        if not exec_select(fmt(
            "SELECT c FROM sbtest WHERE id BETWEEN %d AND %d",
            lo, lo + R - 1)) then return false end
        lo = 1 + lcg_rand(N)       -- sum range
        if not exec_select(fmt(
            "SELECT SUM(k) FROM sbtest WHERE id BETWEEN %d AND %d",
            lo, lo + R - 1)) then return false end
        lo = 1 + lcg_rand(N)       -- order range
        if not exec_select(fmt(
            "SELECT c FROM sbtest WHERE id BETWEEN %d AND %d ORDER BY c",
            lo, lo + R - 1)) then return false end
        lo = 1 + lcg_rand(N)       -- distinct range
        if not exec_select(fmt(
            "SELECT DISTINCT c FROM sbtest WHERE id BETWEEN %d AND %d ORDER BY c",
            lo, lo + R - 1)) then return false end
        if not exec_dml(fmt("UPDATE sbtest SET k = k + 1 WHERE id = %d",
                            1 + lcg_rand(N))) then return false end
        if not exec_dml(fmt("UPDATE sbtest SET c = '%s' WHERE id = %d",
                            C_VAL, 1 + lcg_rand(N))) then return false end
        local id = 1 + lcg_rand(N) -- delete + insert (same id; k drawn with it)
        local k = lcg_rand(N)
        if not exec_dml(fmt("DELETE FROM sbtest WHERE id = %d", id)) then
            return false
        end
        if not exec_dml(fmt(
            "INSERT INTO sbtest (id, k, c, pad) VALUES (%d, %d, '%s', '%s')",
            id, k, C_VAL, PAD_VAL)) then return false end
        if not exec_dml("COMMIT") then return false end
        return true
    end

    -- Retry wrapper: 1213/1205/1062 anywhere in the transaction (BEGIN and
    -- COMMIT included) roll back (ignoring rollback errors), count a retry
    -- and re-run the whole transaction with fresh draws.
    local retries = 0
    local function run_txn()
        while not txn_once() do
            mysql.mysql_real_query(conn, "ROLLBACK", 8) -- result ignored
            retries = retries + 1
        end
    end

    -- Warmup (untimed, before the barrier; retry logic active).
    for _ = 1, cfg.warmup_txns do
        run_txn()
    end
    retries = 0 -- report retries from the timed window only

    -- Barrier: signal ready, then block until the parent sends the start byte.
    if C.write(ready_w, "R", 1) ~= 1 then
        wfail("failed to write ready byte to parent (fd " .. ready_w ..
              ", errno " .. ffi.errno() .. ")")
    end
    local buf = ffi.new("char[1]")
    local nread = C.read(start_r, buf, 1) -- blocking read: this is the barrier
    if nread ~= 1 or buf[0] ~= 83 then    -- 83 = 'S'
        wfail("did not receive start signal from parent")
    end

    -- Timed run: per-txn latency in integer microseconds (truncated), from
    -- just before the first BEGIN to after the successful COMMIT, retries
    -- included.
    local txns
    local lat = {}
    if cfg.duration_s > 0 then
        -- Duration mode: loop until start + BENCH_DURATION_S, checking the
        -- deadline BEFORE each new transaction; an in-flight transaction
        -- (retries included) runs to commit. Deadline math stays in int64
        -- cdata (now_ns() returns int64; the LL literal promotes the product).
        local deadline = now_ns() + cfg.duration_s * 1000000000LL
        txns = 0
        while now_ns() < deadline do
            local t0 = now_ns()
            run_txn()
            txns = txns + 1
            lat[txns] = tonumber((now_ns() - t0) / 1000LL) -- truncating int64 div
        end
    else
        -- Events mode: exactly txns_per_thread transactions; latency array
        -- pre-allocated.
        txns = cfg.txns_per_thread
        for i = 1, txns do lat[i] = 0 end
        for i = 1, txns do
            local t0 = now_ns()
            run_txn()
            lat[i] = tonumber((now_ns() - t0) / 1000LL) -- int64 division truncates
        end
    end

    mysql.mysql_close(conn)
    mysql.mysql_server_end()

    -- Result file: one latency per line, then "retries=<n>". Flush and close
    -- explicitly BEFORE _exit (which skips stdio flushing).
    local path = result_file_path(tonumber(C.getpid()))
    local f = io.open(path, "w")
    if not f then
        wfail("failed to open result file: " .. path)
    end
    local lines = {}
    for i = 1, txns do lines[i] = fmt("%d", lat[i]) end
    lines[txns + 1] = "retries=" .. retries
    if not f:write(table.concat(lines, "\n"), "\n") then
        wfail("failed to write result file: " .. path)
    end
    f:flush()
    if not f:close() then
        wfail("failed to close result file: " .. path)
    end
    C._exit(0)
end

---------------------------------------------------------------------------
-- Parent: fork workers, two-pipe barrier each, merge results, print JSON.
---------------------------------------------------------------------------

local SIGPIPE = 13
C.signal(SIGPIPE, ffi.cast("sighandler_t", 1)) -- SIG_IGN: a dead child must
                                               -- not kill the parent

local threads = cfg.threads
local ready_r = {} -- parent side of each worker's ready pipe (read end)
local start_w = {} -- parent side of each worker's start pipe (write end)
local pids = {}

-- fork() must never run inside a JIT-compiled trace: once this loop gets hot
-- (LuaJIT hotloop threshold ~56 iterations), forking from compiled code
-- corrupts the parent's fd bookkeeping (observed deterministically at worker
-- 58: the next pipe() skips fds the trace failed to release, and the child's
-- own pipe end gets closed by its cleanup loop). Compile-off this function
-- only; worker_main and the benchmark loop still JIT normally.
local function spawn_all()
    local fds = ffi.new("int[2]")
    for i = 1, threads do
        -- Both pipes are created just before this worker's fork.
        if C.pipe(fds) ~= 0 then fail("pipe() failed") end
        local rr, rw = fds[0], fds[1] -- ready pipe: child writes, parent reads
        if C.pipe(fds) ~= 0 then fail("pipe() failed") end
        local sr, sw = fds[0], fds[1] -- start pipe: parent writes, child reads
        ready_r[i] = rr
        start_w[i] = sw

        local pid = C.fork()
        if pid < 0 then fail("fork() failed") end
        if pid == 0 then
            -- Child: close every parent-side fd inherited so far — all earlier
            -- workers' ready-read/start-write ends plus this worker's own two —
            -- keeping only its ready-write and start-read ends.
            for j = 1, i do
                C.close(ready_r[j])
                C.close(start_w[j])
            end
            worker_main(i - 1, rw, sr) -- never returns
            C._exit(1)                 -- unreachable safety net
        end
        -- Parent: close the child-side ends right away.
        C.close(rw)
        C.close(sr)
        pids[i] = pid
    end
end
jit.off(spawn_all)
spawn_all()

local function close_all_parent_fds()
    for i = 1, threads do
        if ready_r[i] then C.close(ready_r[i]); ready_r[i] = nil end
        if start_w[i] then C.close(start_w[i]); start_w[i] = nil end
    end
end

-- Reap every child; returns true when all exited normally with status 0.
local function reap_all()
    local status = ffi.new("int[1]")
    local all_ok = true
    for i = 1, threads do
        local r = C.waitpid(pids[i], status, 0)
        if r < 0 or bit.band(status[0], 0x7f) ~= 0       -- not WIFEXITED
            or bit.band(bit.rshift(status[0], 8), 0xff) ~= 0 then -- WEXITSTATUS
            io.stderr:write("lua worker " .. (i - 1) .. " exited abnormally\n")
            all_ok = false
        end
    end
    return all_ok
end

local function remove_result_files()
    for i = 1, threads do
        os.remove(result_file_path(pids[i]))
    end
end

-- Barrier: wait for every worker's ready byte.
local rbuf = ffi.new("char[1]")
for i = 1, threads do
    local n = C.read(ready_r[i], rbuf, 1) -- blocks; 0 (EOF) if the child died
    if n ~= 1 or rbuf[0] ~= 82 then       -- 82 = 'R'
        io.stderr:write("lua worker " .. (i - 1) .. " did not become ready\n")
        close_all_parent_fds() -- EOF unblocks children waiting for the start byte
        reap_all()
        remove_result_files()
        os.exit(1)
    end
end

-- Release: sample t_start, send the start byte to every worker.
local t_start = now_ns()
for i = 1, threads do
    C.write(start_w[i], "S", 1) -- a failed write means a dead child;
end                             -- waitpid catches it below

-- Join all workers, then sample t_end.
local all_ok = reap_all()
local t_end = now_ns()

close_all_parent_fds()
if not all_ok then
    remove_result_files()
    os.exit(1)
end

-- Merge per-worker result files.
local latencies = {}
local total_retries = 0
local total_txns = 0
for i = 1, threads do
    local path = result_file_path(pids[i])
    local f = io.open(path, "r")
    if not f then
        fail("missing result file for worker " .. (i - 1) .. ": " .. path)
    end
    local data = f:read("*a")
    f:close()
    os.remove(path)
    local count = 0
    local retr = nil
    for line in data:gmatch("[^\n]+") do
        local rv = line:match("^retries=(%d+)$")
        if rv ~= nil then
            retr = tonumber(rv)
        else
            local v = tonumber(line)
            if v == nil or v ~= math.floor(v) then
                fail("malformed result file for worker " .. (i - 1) .. ": " .. path)
            end
            count = count + 1
            latencies[#latencies + 1] = v
        end
    end
    if retr == nil then
        fail("malformed result file for worker " .. (i - 1) .. ": " .. path)
    end
    if cfg.duration_s > 0 then
        if count < 1 then
            fail("worker " .. (i - 1) .. " reported no latencies: " .. path)
        end
    elseif count ~= cfg.txns_per_thread then
        fail("worker " .. (i - 1) .. " reported " .. count ..
             " latencies, expected " .. cfg.txns_per_thread)
    end
    total_txns = total_txns + count
    total_retries = total_retries + retr
end

-- Statistics: sort ascending; SPEC's 0-based p(x) = arr[ceil(x/100 * n) - 1]
-- maps to the 1-based index ceil(x*n/100) = floor((x*n + 99) / 100).
table.sort(latencies)
local n = #latencies
local function pct(x)
    return latencies[math.floor((x * n + 99) / 100)]
end
local max_us = latencies[n]

-- Events mode: total_txns == threads * txns_per_thread (validated above).
-- Duration mode: total committed timed transactions across workers.
local txns = total_txns
local elapsed_s = tonumber(t_end - t_start) / 1e9
local tps = txns / elapsed_s

local hostbuf = ffi.new("char[256]")
local client_host = "unknown"
if C.gethostname(hostbuf, 255) == 0 then
    client_host = ffi.string(hostbuf)
end

local driver_version = ffi.string(mysql.mysql_get_client_info())

io.write(string.format(
    '{"lang":"lua","driver":"libmysqlclient (LuaJIT FFI)",' ..
    '"driver_version":"%s","runtime_version":"%s","prepared":false,' ..
    '"client_host":"%s","db_host":"%s","threads":%d,"txns":%d,' ..
    '"retries":%d,"errors":0,"elapsed_s":%.3f,"tps":%.1f,"qps":%.1f,' ..
    '"p50_us":%d,"p95_us":%d,"p99_us":%d,"max_us":%d}\n',
    driver_version, jit.version, client_host, cfg.host,
    threads, txns, total_retries, elapsed_s, tps, tps * 20,
    pct(50), pct(95), pct(99), max_us))
