// Go implementation of the v2 multithreaded oltp_read_write benchmark (SPEC.md).
//
// Workers are goroutines (Go's native concurrency unit); each takes a dedicated
// session via db.Conn(ctx) and prepares its statements on that conn — no pool
// sharing. BEGIN/COMMIT/ROLLBACK are plain queries (autocommit stays ON at the
// session level); the 18 inner statements are server-side prepared
// (interpolateParams=false, database/sql Prepare).
package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"runtime"
	"sort"
	"strconv"
	"sync"
	"time"

	"github.com/go-sql-driver/mysql"
)

// Must match the version pinned in go.mod.
const driverVersion = "1.9.3"

// ---- configuration, set in main() before any worker starts ----
var (
	tableRows     int64
	threads       int
	warmupTxns    int
	txnsPerThread int
	rangeSize     int64
	seed          int64
	cVal          string // "abcdefghij" x 12 (120 chars)
	padVal        string // "0123456789" x 6  (60 chars)
)

func envStr(name string) string {
	v, ok := os.LookupEnv(name)
	if !ok {
		fmt.Fprintf(os.Stderr, "missing required environment variable: %s\n", name)
		os.Exit(1)
	}
	return v
}

func envInt(name string) int64 {
	v := envStr(name)
	n, err := strconv.ParseInt(v, 10, 64)
	if err != nil {
		fmt.Fprintf(os.Stderr, "environment variable %s is not a valid integer: %q\n", name, v)
		os.Exit(1)
	}
	return n
}

func repeat(s string, times int) string {
	out := ""
	for i := 0; i < times; i++ {
		out += s
	}
	return out
}

// p(x) = arr[ceil(x/100 * n) - 1], 0-based, array sorted ascending.
func percentile(sorted []int64, x int) int64 {
	n := len(sorted)
	idx := (x*n+99)/100 - 1
	if idx < 0 {
		idx = 0
	}
	return sorted[idx]
}

type worker struct {
	idx       int
	lcgState  uint64  // 31-bit LCG per SPEC.md, one independent stream per worker
	latencies []int64 // pre-allocated, one slot per timed txn, integer microseconds
	retries   int64   // retryable-error count (timed window only; reset after warmup)

	conn *sql.Conn

	pointSel    *sql.Stmt
	rangeSel    *sql.Stmt
	sumSel      *sql.Stmt
	orderSel    *sql.Stmt
	distinctSel *sql.Stmt
	idxUpd      *sql.Stmt
	nonIdxUpd   *sql.Stmt
	del         *sql.Stmt
	ins         *sql.Stmt
}

// rand returns a uniform value in 0..n-1 and advances the LCG.
func (w *worker) rand(n int64) int64 {
	w.lcgState = (w.lcgState*1103515245 + 12345) % (1 << 31)
	return int64(w.lcgState % uint64(n))
}

func (w *worker) fail(err error) {
	fmt.Fprintf(os.Stderr, "worker %d error: %v\n", w.idx, err)
	os.Exit(1)
}

// fetchAll executes a prepared SELECT and fetches every returned row, scanning
// the single column into sql.RawBytes. The result set is fully drained and
// closed before returning — required before the next statement on this conn.
func fetchAll(ctx context.Context, st *sql.Stmt, args ...any) error {
	rows, err := st.QueryContext(ctx, args...)
	if err != nil {
		return err
	}
	var v sql.RawBytes
	for rows.Next() {
		if err := rows.Scan(&v); err != nil {
			rows.Close()
			return err
		}
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return err
	}
	return rows.Close()
}

// fetchSum is fetchAll for SELECT SUM(k): the DECIMAL (possibly NULL) result
// is scanned into sql.NullString.
func fetchSum(ctx context.Context, st *sql.Stmt, args ...any) error {
	rows, err := st.QueryContext(ctx, args...)
	if err != nil {
		return err
	}
	var v sql.NullString
	for rows.Next() {
		if err := rows.Scan(&v); err != nil {
			rows.Close()
			return err
		}
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return err
	}
	return rows.Close()
}

// txnOnce runs one sysbench oltp_read_write transaction; values are drawn
// immediately before executing each statement (draw order = execution order).
func (w *worker) txnOnce(ctx context.Context) error {
	if _, err := w.conn.ExecContext(ctx, "BEGIN"); err != nil {
		return err
	}

	for i := 0; i < 10; i++ {
		id := 1 + w.rand(tableRows)
		if err := fetchAll(ctx, w.pointSel, id); err != nil {
			return err
		}
	}

	lo := 1 + w.rand(tableRows)
	if err := fetchAll(ctx, w.rangeSel, lo, lo+rangeSize-1); err != nil {
		return err
	}

	lo = 1 + w.rand(tableRows)
	if err := fetchSum(ctx, w.sumSel, lo, lo+rangeSize-1); err != nil {
		return err
	}

	lo = 1 + w.rand(tableRows)
	if err := fetchAll(ctx, w.orderSel, lo, lo+rangeSize-1); err != nil {
		return err
	}

	lo = 1 + w.rand(tableRows)
	if err := fetchAll(ctx, w.distinctSel, lo, lo+rangeSize-1); err != nil {
		return err
	}

	id := 1 + w.rand(tableRows)
	if _, err := w.idxUpd.ExecContext(ctx, id); err != nil {
		return err
	}

	id = 1 + w.rand(tableRows)
	if _, err := w.nonIdxUpd.ExecContext(ctx, cVal, id); err != nil {
		return err
	}

	id = 1 + w.rand(tableRows)
	k := w.rand(tableRows)
	if _, err := w.del.ExecContext(ctx, id); err != nil {
		return err
	}
	if _, err := w.ins.ExecContext(ctx, id, k, cVal, padVal); err != nil {
		return err
	}

	_, err := w.conn.ExecContext(ctx, "COMMIT")
	return err
}

// runTxn runs one transaction, retrying on deadlock (1213), lock wait timeout
// (1205) and duplicate key (1062) with all values redrawn (the PRNG stream
// continues). Any other error aborts the whole benchmark.
func (w *worker) runTxn(ctx context.Context) {
	for {
		err := w.txnOnce(ctx)
		if err == nil {
			return
		}
		var me *mysql.MySQLError
		if errors.As(err, &me) && (me.Number == 1213 || me.Number == 1205 || me.Number == 1062) {
			w.conn.ExecContext(ctx, "ROLLBACK") // rollback errors are ignored per spec
			w.retries++
			continue // re-run the whole transaction with fresh draws
		}
		w.fail(err)
	}
}

func (w *worker) run(ctx context.Context, db *sql.DB, ready, done *sync.WaitGroup, start <-chan struct{}) {
	var err error
	w.conn, err = db.Conn(ctx) // dedicated session for this worker
	if err != nil {
		w.fail(err)
	}

	prep := func(query string) *sql.Stmt {
		st, err := w.conn.PrepareContext(ctx, query)
		if err != nil {
			w.fail(err)
		}
		return st
	}
	w.pointSel = prep("SELECT c FROM sbtest WHERE id = ?")
	w.rangeSel = prep("SELECT c FROM sbtest WHERE id BETWEEN ? AND ?")
	w.sumSel = prep("SELECT SUM(k) FROM sbtest WHERE id BETWEEN ? AND ?")
	w.orderSel = prep("SELECT c FROM sbtest WHERE id BETWEEN ? AND ? ORDER BY c")
	w.distinctSel = prep("SELECT DISTINCT c FROM sbtest WHERE id BETWEEN ? AND ? ORDER BY c")
	w.idxUpd = prep("UPDATE sbtest SET k = k + 1 WHERE id = ?")
	w.nonIdxUpd = prep("UPDATE sbtest SET c = ? WHERE id = ?")
	w.del = prep("DELETE FROM sbtest WHERE id = ?")
	w.ins = prep("INSERT INTO sbtest (id, k, c, pad) VALUES (?, ?, ?, ?)")

	for i := 0; i < warmupTxns; i++ {
		w.runTxn(ctx)
	}
	w.retries = 0 // reported retries cover the timed window only

	ready.Done()
	<-start // released together with main's t_start sample

	for i := 0; i < txnsPerThread; i++ {
		t0 := time.Now()
		w.runTxn(ctx)
		w.latencies[i] = time.Since(t0).Microseconds() // truncated integer µs, retries included
	}

	w.conn.Close()
	done.Done()
}

func main() {
	host := envStr("MYSQL_HOST")
	port := envStr("MYSQL_PORT")
	user := envStr("MYSQL_USER")
	password := envStr("MYSQL_PASSWORD")
	db := envStr("MYSQL_DB")

	tableRows = envInt("BENCH_TABLE_ROWS")
	threads = int(envInt("BENCH_THREADS"))
	warmupTxns = int(envInt("BENCH_WARMUP_TXNS"))
	txnsPerThread = int(envInt("BENCH_TXNS_PER_THREAD"))
	rangeSize = envInt("BENCH_RANGE_SIZE")
	seed = envInt("BENCH_SEED")
	if tableRows <= 0 || threads <= 0 || txnsPerThread <= 0 || warmupTxns < 0 || rangeSize <= 0 {
		fmt.Fprintln(os.Stderr, "invalid workload configuration: BENCH_TABLE_ROWS, BENCH_THREADS, BENCH_TXNS_PER_THREAD, BENCH_RANGE_SIZE must be > 0 and BENCH_WARMUP_TXNS >= 0")
		os.Exit(1)
	}

	cVal = repeat("abcdefghij", 12)
	padVal = repeat("0123456789", 6)

	// TCP, TLS disabled (TLSConfig empty = none), server-side prepared
	// statements (InterpolateParams=false); loc/charset left at driver defaults.
	cfg := mysql.NewConfig()
	cfg.Net = "tcp"
	cfg.Addr = host + ":" + port
	cfg.User = user
	cfg.Passwd = password
	cfg.DBName = db
	cfg.InterpolateParams = false
	cfg.TLSConfig = "false"

	pool, err := sql.Open("mysql", cfg.FormatDSN())
	if err != nil {
		fmt.Fprintf(os.Stderr, "sql.Open error: %v\n", err)
		os.Exit(1)
	}
	pool.SetMaxOpenConns(threads + 2) // the pool must never cap the worker count
	pool.SetMaxIdleConns(threads + 2)
	pool.SetConnMaxLifetime(0)

	ctx := context.Background()
	var ready, done sync.WaitGroup
	ready.Add(threads)
	done.Add(threads)
	start := make(chan struct{})

	workers := make([]*worker, threads)
	for i := 0; i < threads; i++ {
		workers[i] = &worker{
			idx:       i,
			lcgState:  uint64(seed + int64(i)), // worker i seeds with BENCH_SEED + i
			latencies: make([]int64, txnsPerThread),
		}
		go workers[i].run(ctx, pool, &ready, &done, start)
	}

	ready.Wait() // all workers connected, prepared, warmed up
	tStart := time.Now()
	close(start)
	done.Wait()
	elapsed := time.Since(tStart)

	txns := threads * txnsPerThread
	all := make([]int64, 0, txns)
	var totalRetries int64
	for _, w := range workers {
		all = append(all, w.latencies...)
		totalRetries += w.retries
	}
	sort.Slice(all, func(i, j int) bool { return all[i] < all[j] })

	elapsedS := elapsed.Seconds()
	tps := float64(txns) / elapsedS
	qps := tps * 20.0 // 18 statements + BEGIN + COMMIT, sysbench counting

	clientHost, err := os.Hostname()
	if err != nil || clientHost == "" {
		clientHost = "unknown"
	}

	fmt.Fprintf(os.Stdout,
		`{"lang":"go","driver":"go-sql-driver/mysql","driver_version":"%s","runtime_version":"%s","prepared":true,"client_host":"%s","db_host":"%s","threads":%d,"txns":%d,"retries":%d,"errors":0,"elapsed_s":%.3f,"tps":%.1f,"qps":%.1f,"p50_us":%d,"p95_us":%d,"p99_us":%d,"max_us":%d}`+"\n",
		driverVersion, runtime.Version(), clientHost, host,
		threads, txns, totalRetries,
		elapsedS, tps, qps,
		percentile(all, 50), percentile(all, 95), percentile(all, 99), all[len(all)-1])
}
