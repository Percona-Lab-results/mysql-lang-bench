-- mysql-lang-bench — Lua implementation (v3: four micro-workloads).
--
-- LuaJIT FFI over libmysqlclient, TEXT protocol only: mysql_stmt_* is
-- deliberately not used (the MYSQL_BIND struct layout is version-fragile in
-- FFI), so every statement is sent via mysql_real_query and "prepared" is
-- reported false (Python-style). Blob payloads are escaped with
-- mysql_real_escape_string. See SPEC.md.
--
-- One of four workloads runs per process, selected by BENCH_WORKLOAD:
--   1 int-insert   multi-row INSERT of BENCH_INT_BATCH literal ints
--   2 int-read     SELECT val ... LIMIT n, fetch all, sum into int64
--   3 blob-insert  bulk-fill a BENCH_BLOB_SIZE buffer once, re-id per blob,
--                  MD5, INSERT escaped
--   4 blob-read    SELECT data WHERE id=?, fetch blob, MD5 it
--
-- Workers are forked processes (Lua has no threads), PHP-style:
--   - two pipes per worker (ready-pipe, start-pipe), created just before fork;
--   - the child connects to MySQL (connections are created only AFTER fork;
--     the parent never connects), writes one ready byte, then blocks reading
--     one start byte (the barrier). No warm-up, no prepare step;
--   - the parent reads one ready byte per child (EOF = child died -> abort),
--     samples t_start, writes the start byte to every child, then reaps every
--     child (non-blocking waitpid + sleep while publishing progress), samples
--     t_end;
--   - each child writes a header line "ops=<n> chk=<sink>" then one integer
--     microsecond latency value per line to /tmp/bench-lua-<pid>.result and
--     _exit(0)s; the parent merges those files and prints the JSON result line.
--
-- Intermediate progress (SPEC "Intermediate progress"): if BENCH_PROGRESS_FILE
-- is set, each child writes its completed-op count (rows for WL1/2, blobs for
-- WL3/4) every BENCH_PROGRESS_SEC seconds to BENCH_PROGRESS_FILE.w<i>; the
-- parent sums those temps and atomically overwrites BENCH_PROGRESS_FILE with a
-- partial JSON line until all children exit, then deletes it on success.

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
int nanosleep(const struct timespec *req, struct timespec *rem);
int fork(void);
int pipe(int pipefd[2]);
long read(int fd, char *buf, unsigned long count);
long write(int fd, const char *buf, unsigned long count);
int close(int fd);
int waitpid(int pid, int *status, int options);
int getpid(void);
void _exit(int status);
int gethostname(char *name, unsigned long len);
long long strtoll(const char *nptr, char **endptr, int base);
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
unsigned long mysql_real_escape_string(MYSQL *mysql, char *to,
                                       const char *from, unsigned long length);
MYSQL_RES *mysql_store_result(MYSQL *mysql);
char **mysql_fetch_row(MYSQL_RES *result);
unsigned long *mysql_fetch_lengths(MYSQL_RES *result);
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

/* ---- OpenSSL libcrypto: one-shot MD5 (16-byte digest) ---- */
unsigned char *MD5(const unsigned char *d, size_t n, unsigned char *md);
]]

local C = ffi.C

local function load_lib(label, candidates, hint)
    for _, name in ipairs(candidates) do
        local ok, lib = pcall(ffi.load, name)
        if ok then return lib end
    end
    io.stderr:write("failed to load " .. label .. " (tried " ..
                    table.concat(candidates, ", ") .. "): " .. hint .. "\n")
    os.exit(1)
end

local mysql = load_lib("libmysqlclient",
    { "mysqlclient", "libmysqlclient.so.24", "libmysqlclient.so.21",
      "libmysqlclient.so" },
    "install libmysqlclient-dev / libmysqlclient21+")
local crypto = load_lib("libcrypto (OpenSSL)",
    { "crypto", "libcrypto.so.3", "libcrypto.so.1.1", "libcrypto.so" },
    "install libssl-dev / libssl3")

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

local progress_file = os.getenv("BENCH_PROGRESS_FILE")
if progress_file == "" then progress_file = nil end

local cfg = {
    host         = env_str("MYSQL_HOST"),
    port         = env_int("MYSQL_PORT"),
    user         = env_str("MYSQL_USER"),
    password     = password,
    db           = env_str("MYSQL_DB"),
    workload     = env_int("BENCH_WORKLOAD"),
    threads      = env_int("BENCH_THREADS"),
    seed         = env_int("BENCH_SEED"),
    int_rows     = env_int_opt("BENCH_INT_ROWS", 1000000),
    int_batch    = env_int_opt("BENCH_INT_BATCH", 10),
    blob_count   = env_int_opt("BENCH_BLOB_COUNT", 100),
    blob_size    = env_int_opt("BENCH_BLOB_SIZE", 1048576),
    progress_sec = env_int_opt("BENCH_PROGRESS_SEC", 5),
}

if cfg.workload < 1 or cfg.workload > 4 then
    fail("BENCH_WORKLOAD must be 1..4")
end
if cfg.threads < 1 or cfg.seed < 0 or cfg.int_rows < 1 or cfg.int_batch < 1
    or cfg.blob_count < 1 or cfg.blob_size < 1 or cfg.progress_sec < 1 then
    fail("invalid workload configuration")
end

local WL_NAME = { "int-insert", "int-read", "blob-insert", "blob-read" }
local WL_UNIT = { "rows/s", "rows/s", "blobs/s", "blobs/s" }
local workload   = cfg.workload
local wl_name    = WL_NAME[workload]
local wl_unit    = WL_UNIT[workload]
local threads    = cfg.threads

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

    if mysql.mysql_server_init(0, nil, nil) ~= 0 then
        wfail("mysql_server_init failed")
    end

    -- Connection (child only, after fork): TCP, TLS disabled, autocommit ON.
    local conn = mysql.mysql_init(nil)
    if conn == nil then
        wfail("mysql_init failed (out of memory)")
    end
    -- Option constants from the MySQL headers (enum mysql_option / ssl_mode).
    local proto = ffi.new("unsigned int[1]", 1)    -- MYSQL_PROTOCOL_TCP
    mysql.mysql_options(conn, 9, proto)             -- MYSQL_OPT_PROTOCOL
    local ssl_mode = ffi.new("unsigned int[1]", 1)  -- SSL_MODE_DISABLED
    mysql.mysql_options(conn, 35, ssl_mode)         -- MYSQL_OPT_SSL_MODE
    local get_pk = ffi.new("bool[1]", true)
    mysql.mysql_options(conn, 36, get_pk)           -- MYSQL_OPT_GET_SERVER_PUBLIC_KEY

    if mysql.mysql_real_connect(conn, cfg.host, cfg.user, cfg.password,
                                cfg.db, cfg.port, nil, 0) == nil then
        wfail(fmt("mysql_real_connect failed: (%d) %s",
                  tonumber(mysql.mysql_errno(conn)),
                  ffi.string(mysql.mysql_error(conn))))
    end

    local function die_sql(what)
        wfail(fmt("%s failed: (%d) %s", what,
                  tonumber(mysql.mysql_errno(conn)),
                  ffi.string(mysql.mysql_error(conn))))
    end

    -- Any SQL error aborts (SPEC: errors always 0, no retries).
    local function exec(sql, len)
        if mysql.mysql_real_query(conn, sql, len) ~= 0 then die_sql(sql) end
    end

    -- PRNG: 31-bit LCG, one independent stream per worker, seeded with
    -- BENCH_SEED + worker_idx. All LCG math stays in uint64 cdata: plain Lua
    -- numbers are doubles (53-bit mantissa) and state*1103515245 would lose
    -- bits. next() results are < 2^31, exact as doubles after tonumber().
    local state = 0ULL + (cfg.seed + worker_idx)

    -- Progress bookkeeping: completed ops published to a per-child temp file
    -- (atomic write + rename) no more often than every BENCH_PROGRESS_SEC.
    local completed = 0
    local prog_interval = cfg.progress_sec * 1000000000LL
    local last_prog = now_ns()
    local wpath = progress_file and (progress_file .. ".w" .. worker_idx)
    local function write_progress()
        local f = io.open(wpath .. ".tmp", "w")
        if not f then return end
        f:write(fmt("%d", completed))
        f:close()
        os.rename(wpath .. ".tmp", wpath)
    end
    local function maybe_progress()
        if not wpath then return end
        local nowp = now_ns()
        if nowp - last_prog < prog_interval then return end
        last_prog = nowp
        write_progress()
    end

    -- Barrier: signal ready, then block until the parent sends the start byte.
    if C.write(ready_w, "R", 1) ~= 1 then
        wfail("failed to write ready byte to parent (fd " .. ready_w ..
              ", errno " .. ffi.errno() .. ")")
    end
    local sbuf = ffi.new("char[1]")
    local nread = C.read(start_r, sbuf, 1) -- blocking read: this is the barrier
    if nread ~= 1 or sbuf[0] ~= 83 then    -- 83 = 'S'
        wfail("did not receive start signal from parent")
    end

    -- Timed phase. lat[] holds one integer-microsecond sample per operation.
    local lat = {}
    local ops_done = 0
    local chk = 0LL -- black-box sink: written to the result file so neither
                    -- the sum nor the MD5 digests can be optimized away.

    if workload == 1 then
        ----------------------------------------------------------------
        -- WL1 int-insert: BENCH_INT_ROWS / BENCH_INT_BATCH multi-row
        -- INSERTs of literal signed-32-bit ints. Autocommit ON — each
        -- INSERT is its own commit, no wrapping transaction (SPEC).
        ----------------------------------------------------------------
        local batch = cfg.int_batch
        local n_stmts = math.floor(cfg.int_rows / batch)
        local parts = {}
        for j = 1, batch do parts[j] = "(%d)" end
        local sql_fmt = "INSERT INTO bench_int (val) VALUES " ..
                        table.concat(parts, ",")
        local vals = {}

        for _ = 1, n_stmts do
            for j = 1, batch do
                state = (state * 1103515245ULL + 12345ULL) % 2147483648ULL
                vals[j] = tonumber(state) -- next() in [0, 2^31-1]: fits int32
            end
            local sql = fmt(sql_fmt, unpack(vals, 1, batch))
            local t0 = now_ns()
            exec(sql, #sql)
            lat[#lat + 1] = tonumber((now_ns() - t0) / 1000LL)
            completed = completed + batch
            maybe_progress()
        end
        ops_done = n_stmts * batch

    elseif workload == 2 then
        ----------------------------------------------------------------
        -- WL2 int-read: one SELECT ... LIMIT n, fetch every row, sum val
        -- into a 64-bit accumulator (fed to the result-file sink).
        ----------------------------------------------------------------
        local sql = "SELECT val FROM bench_int LIMIT " .. cfg.int_rows
        local rows = 0
        local t0 = now_ns()
        exec(sql, #sql)
        local res = mysql.mysql_store_result(conn)
        if res == nil then die_sql("mysql_store_result") end
        while true do
            local row = mysql.mysql_fetch_row(res)
            if row == nil then break end
            chk = chk + C.strtoll(row[0], nil, 10) -- val is NOT NULL
            rows = rows + 1
            if rows % 65536 == 0 then
                completed = rows
                maybe_progress()
            end
        end
        mysql.mysql_free_result(res)
        lat[1] = tonumber((now_ns() - t0) / 1000LL)
        completed = rows
        ops_done = rows

    elseif workload == 3 then
        ----------------------------------------------------------------
        -- WL3 blob-insert: build the BENCH_BLOB_SIZE payload ONCE with a
        -- fast bulk LCG fill (one 64-bit store per step, compiled, not a
        -- per-byte interpreted loop), then before each blob overwrite its
        -- first 8 bytes with the blob id so every blob's content and MD5
        -- differ. MD5 + escape + INSERT the full buffer each time.
        -- Autocommit ON — each INSERT is its own commit (SPEC). id = i*COUNT + k.
        ----------------------------------------------------------------
        local size = cfg.blob_size
        local count = cfg.blob_count
        -- Over-allocate to a multiple of 8 so the word fill never overruns;
        -- only the first `size` bytes are ever MD5'd / escaped / inserted.
        local words = math.ceil(size / 8)
        local data = ffi.new("uint8_t[?]", words * 8)
        local p64 = ffi.cast("uint64_t *", data)
        for j = 0, words - 1 do
            state = (state * 1103515245ULL + 12345ULL) % 2147483648ULL
            p64[j] = state -- 8 bytes per LCG step
        end
        local digest = ffi.new("unsigned char[16]")
        -- Statement buffer: prefix + escaped payload (<= 2*size) + "')".
        local stmt = ffi.new("char[?]", 64 + 2 * size + 8)
        local data_c = ffi.cast("const unsigned char *", data)
        local data_from = ffi.cast("const char *", data)
        for k = 0, count - 1 do
            local id = worker_idx * count + k
            p64[0] = 0ULL + id -- make this blob (and its MD5) unique
            crypto.MD5(data_c, size, digest)
            for b = 0, 15 do chk = chk + digest[b] end
            local prefix = fmt("INSERT INTO bench_blob (id, data) VALUES (%d, '", id)
            local plen = #prefix
            ffi.copy(stmt, prefix, plen)
            local esclen = tonumber(
                mysql.mysql_real_escape_string(conn, stmt + plen, data_from, size))
            stmt[plen + esclen] = 39     -- "'"
            stmt[plen + esclen + 1] = 41 -- ")"
            local total = plen + esclen + 2
            local t0 = now_ns()
            if mysql.mysql_real_query(conn, stmt, total) ~= 0 then
                die_sql("blob INSERT")
            end
            lat[#lat + 1] = tonumber((now_ns() - t0) / 1000LL)
            completed = k + 1
            maybe_progress()
        end
        ops_done = count

    else
        ----------------------------------------------------------------
        -- WL4 blob-read: read this worker's BENCH_BLOB_COUNT blobs by id
        -- range, MD5 each blob's content. id = i*COUNT + k.
        ----------------------------------------------------------------
        local count = cfg.blob_count
        local digest = ffi.new("unsigned char[16]")
        for k = 0, count - 1 do
            local id = worker_idx * count + k
            local sql = fmt("SELECT data FROM bench_blob WHERE id = %d", id)
            local t0 = now_ns()
            exec(sql, #sql)
            local res = mysql.mysql_store_result(conn)
            if res == nil then die_sql("mysql_store_result") end
            local row = mysql.mysql_fetch_row(res)
            if row == nil then wfail("no blob row for id " .. id) end
            local lengths = mysql.mysql_fetch_lengths(res)
            local len = tonumber(lengths[0])
            lat[#lat + 1] = tonumber((now_ns() - t0) / 1000LL)
            crypto.MD5(ffi.cast("const unsigned char *", row[0]), len, digest)
            for b = 0, 15 do chk = chk + digest[b] end
            mysql.mysql_free_result(res)
            completed = k + 1
            maybe_progress()
        end
        ops_done = count
    end

    mysql.mysql_close(conn)
    mysql.mysql_server_end()

    -- Result file: header "ops=<n> chk=<sink>" then one latency per line.
    -- Flush and close explicitly BEFORE _exit (which skips stdio flushing).
    local path = result_file_path(tonumber(C.getpid()))
    local f = io.open(path, "w")
    if not f then
        wfail("failed to open result file: " .. path)
    end
    local lines = {}
    lines[1] = fmt("ops=%d chk=%s", ops_done, tostring(chk))
    for i = 1, #lat do lines[i + 1] = fmt("%d", lat[i]) end
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

local ready_r = {} -- parent side of each worker's ready pipe (read end)
local start_w = {} -- parent side of each worker's start pipe (write end)
local pids = {}

-- fork() must never run inside a JIT-compiled trace: once this loop gets hot
-- (LuaJIT hotloop threshold ~56 iterations), forking from compiled code
-- corrupts the parent's fd bookkeeping (observed deterministically at worker
-- 58: the next pipe() skips fds the trace failed to release, and the child's
-- own pipe end gets closed by its cleanup loop). Compile-off this function
-- only; worker_main and the benchmark loops still JIT normally.
local function spawn_all()
    local fds = ffi.new("int[2]")
    for i = 1, threads do
        if C.pipe(fds) ~= 0 then fail("pipe() failed") end
        local rr, rw = fds[0], fds[1] -- ready pipe: child writes, parent reads
        if C.pipe(fds) ~= 0 then fail("pipe() failed") end
        local sr, sw = fds[0], fds[1] -- start pipe: parent writes, child reads
        ready_r[i] = rr
        start_w[i] = sw

        local pid = C.fork()
        if pid < 0 then fail("fork() failed") end
        if pid == 0 then
            -- Child: close every parent-side fd inherited so far, keeping only
            -- its own ready-write and start-read ends.
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

local function remove_result_files()
    for i = 1, threads do
        os.remove(result_file_path(pids[i]))
    end
end

-- Remove per-child progress temps (BENCH_PROGRESS_FILE.w<i>) and any stray tmp.
local function remove_progress_temps()
    if not progress_file then return end
    for i = 0, threads - 1 do
        os.remove(progress_file .. ".w" .. i)
        os.remove(progress_file .. ".w" .. i .. ".tmp")
    end
    os.remove(progress_file .. ".tmp")
end

-- Sum the per-child completed-op counts published to the progress temps.
local function sum_child_progress()
    local total = 0
    for i = 0, threads - 1 do
        local f = io.open(progress_file .. ".w" .. i, "r")
        if f then
            local s = f:read("*a")
            f:close()
            local v = tonumber(s)
            if v then total = total + v end
        end
    end
    return total
end

-- Atomically overwrite BENCH_PROGRESS_FILE with one partial-result JSON line.
local function write_parent_progress(t_start)
    local ops = sum_child_progress()
    local elapsed = tonumber(now_ns() - t_start) / 1e9
    local thr = elapsed > 0 and ops / elapsed or 0
    local line = string.format(
        '{"workload":%d,"workload_name":"%s","unit":"%s","threads":%d,' ..
        '"ops":%d,"elapsed_s":%.3f,"throughput":%.1f,"partial":true}',
        workload, wl_name, wl_unit, threads, ops, elapsed, thr)
    local f = io.open(progress_file .. ".tmp", "w")
    if not f then return end
    f:write(line, "\n")
    f:close()
    os.rename(progress_file .. ".tmp", progress_file)
end

-- Barrier: wait for every worker's ready byte.
local rbuf = ffi.new("char[1]")
for i = 1, threads do
    local n = C.read(ready_r[i], rbuf, 1) -- blocks; 0 (EOF) if the child died
    if n ~= 1 or rbuf[0] ~= 82 then       -- 82 = 'R'
        io.stderr:write("lua worker " .. (i - 1) .. " did not become ready\n")
        close_all_parent_fds() -- EOF unblocks children waiting for the start byte
        local st = ffi.new("int[1]")
        for j = 1, threads do C.waitpid(pids[j], st, 0) end
        remove_result_files()
        remove_progress_temps()
        os.exit(1)
    end
end

-- Release: sample t_start, send the start byte to every worker.
local t_start = now_ns()
for i = 1, threads do
    C.write(start_w[i], "S", 1) -- a failed write means a dead child;
end                             -- waitpid catches it below

-- Join all workers, sampling t_end once the last one exits.
-- WIFEXITED: (status & 0x7f) == 0 ; WEXITSTATUS: (status >> 8) & 0xff.
local function status_ok(status)
    return bit.band(status, 0x7f) == 0
        and bit.band(bit.rshift(status, 8), 0xff) == 0
end

local WNOHANG = 1
local status = ffi.new("int[1]")
local all_ok = true
local t_end

if progress_file then
    -- Forked-progress path: non-blocking waitpid + sleep, publishing the
    -- summed partial result every BENCH_PROGRESS_SEC until all children exit.
    local reaped = {}
    local remaining = threads
    local sleepts = ffi.new("struct timespec")
    sleepts.tv_sec = 0
    sleepts.tv_nsec = 20000000 -- 20 ms poll granularity
    local last_parent_prog = t_start
    local prog_interval = cfg.progress_sec * 1000000000LL
    while remaining > 0 do
        for i = 1, threads do
            if not reaped[i] then
                local r = C.waitpid(pids[i], status, WNOHANG)
                if r == pids[i] then
                    reaped[i] = true
                    remaining = remaining - 1
                    if not status_ok(status[0]) then
                        io.stderr:write("lua worker " .. (i - 1) ..
                                        " exited abnormally\n")
                        all_ok = false
                    end
                elseif r < 0 then
                    io.stderr:write("waitpid failed for lua worker " ..
                                    (i - 1) .. "\n")
                    reaped[i] = true
                    remaining = remaining - 1
                    all_ok = false
                end
            end
        end
        if remaining == 0 then break end
        local nowp = now_ns()
        if nowp - last_parent_prog >= prog_interval then
            last_parent_prog = nowp
            write_parent_progress(t_start)
        end
        C.nanosleep(sleepts, nil)
    end
    t_end = now_ns()
else
    -- No progress file: blocking reap for an exact t_end.
    for i = 1, threads do
        local r = C.waitpid(pids[i], status, 0)
        if r < 0 or not status_ok(status[0]) then
            io.stderr:write("lua worker " .. (i - 1) .. " exited abnormally\n")
            all_ok = false
        end
    end
    t_end = now_ns()
end

close_all_parent_fds()
if not all_ok then
    remove_result_files()
    remove_progress_temps()
    os.exit(1)
end

-- Merge per-worker result files: header "ops=<n> chk=.." then latency lines.
local latencies = {}
local total_ops = 0
for i = 1, threads do
    local path = result_file_path(pids[i])
    local f = io.open(path, "r")
    if not f then
        fail("missing result file for worker " .. (i - 1) .. ": " .. path)
    end
    local data = f:read("*a")
    f:close()
    os.remove(path)
    local header = nil
    for line in data:gmatch("[^\n]+") do
        if header == nil then
            local ops = line:match("^ops=(%d+)")
            if ops == nil then
                fail("malformed result header for worker " .. (i - 1) ..
                     ": " .. path)
            end
            header = true
            total_ops = total_ops + tonumber(ops)
        else
            local v = tonumber(line)
            if v == nil or v ~= math.floor(v) then
                fail("malformed result file for worker " .. (i - 1) .. ": " .. path)
            end
            latencies[#latencies + 1] = v
        end
    end
    if header == nil then
        fail("empty result file for worker " .. (i - 1) .. ": " .. path)
    end
end

-- Statistics: sort ascending; SPEC's 0-based p(x) = arr[ceil(x/100 * n) - 1]
-- maps to the 1-based index ceil(x*n/100) = floor((x*n + 99) / 100).
table.sort(latencies)
local n = #latencies
if n < 1 then fail("no latency samples collected") end
local function pct(x)
    return latencies[math.floor((x * n + 99) / 100)]
end
local max_us = latencies[n]

local elapsed_s = tonumber(t_end - t_start) / 1e9
local throughput = elapsed_s > 0 and total_ops / elapsed_s or 0
-- MB/s only meaningful for the blob workloads.
local mb_str = "0"
if workload >= 3 and elapsed_s > 0 then
    mb_str = string.format("%.1f",
        total_ops * cfg.blob_size / 1e6 / elapsed_s)
end

local hostbuf = ffi.new("char[256]")
local client_host = "unknown"
if C.gethostname(hostbuf, 255) == 0 then
    client_host = ffi.string(hostbuf)
end

local driver_version = ffi.string(mysql.mysql_get_client_info())

io.write(string.format(
    '{"lang":"lua","workload":%d,"workload_name":"%s","unit":"%s",' ..
    '"driver":"libmysqlclient (LuaJIT FFI)","driver_version":"%s",' ..
    '"runtime_version":"%s","prepared":false,"client_host":"%s",' ..
    '"db_host":"%s","threads":%d,"ops":%d,"errors":0,"elapsed_s":%.3f,' ..
    '"throughput":%.1f,"mb_per_s":%s,"p50_us":%d,"p95_us":%d,"p99_us":%d,' ..
    '"max_us":%d,"client_cpu_s":0,"server_cpu_s":0}\n',
    workload, wl_name, wl_unit, driver_version, jit.version, client_host,
    cfg.host, threads, total_ops, elapsed_s, throughput, mb_str,
    pct(50), pct(95), pct(99), max_us))

-- On success the final JSON on stdout supersedes the progress file; delete it.
if progress_file then
    remove_progress_temps()
    os.remove(progress_file)
end
