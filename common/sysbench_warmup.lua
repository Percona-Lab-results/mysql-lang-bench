-- Read-only sysbench script that warms the server caches for THIS project's
-- `sbtest` table (sysbench's stock oltp_* scripts target sbtest1..N, a
-- different table). Point selects + PK ranges + k-index ranges touch every
-- page of both the clustered and secondary index over time, loading the
-- buffer pool without mutating data — so every language still starts its
-- sweep from byte-identical table state.
--
-- Run:
--   sysbench common/sysbench_warmup.lua --mysql-host=... --mysql-user=... \
--     --time=900 --threads=32 run
-- Table size is read from the BENCH_TABLE_ROWS environment variable.

local table_rows = tonumber(os.getenv("BENCH_TABLE_ROWS")) or 10000000
local range = tonumber(os.getenv("BENCH_RANGE_SIZE")) or 100

function thread_init()
    drv = sysbench.sql.driver()
    con = drv:connect()
end

function thread_done()
    con:disconnect()
end

function event()
    for i = 1, 8 do
        con:query(string.format(
            "SELECT c FROM sbtest WHERE id = %d",
            sysbench.rand.uniform(1, table_rows)))
    end
    local lo = sysbench.rand.uniform(1, table_rows)
    con:query(string.format(
        "SELECT c FROM sbtest WHERE id BETWEEN %d AND %d", lo, lo + range - 1))
    lo = sysbench.rand.uniform(1, table_rows)
    con:query(string.format(
        "SELECT SUM(k) FROM sbtest WHERE id BETWEEN %d AND %d", lo, lo + range - 1))
    -- secondary (k) index
    local k = sysbench.rand.uniform(1, table_rows)
    con:query(string.format(
        "SELECT COUNT(*) FROM sbtest WHERE k BETWEEN %d AND %d", k, k + range - 1))
end
