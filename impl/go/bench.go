// Go implementation of the v3 four-micro-workload benchmark (SPEC.md).
//
// One of four workloads is selected by BENCH_WORKLOAD (1..4). BENCH_THREADS
// workers run concurrently as goroutines; each worker owns a dedicated session
// obtained via db.Conn(ctx) and prepares its statement on that conn — no pool
// sharing. Statements are server-side prepared (interpolateParams=false).
//
//	WL1 int-insert  — prepared multi-row INSERT of BENCH_INT_BATCH rows, run
//	                  BENCH_INT_ROWS/BENCH_INT_BATCH times in one transaction.
//	WL2 int-fetch   — SELECT val FROM bench_int LIMIT ?, sum val in app code.
//	WL3 blob-insert — fill BENCH_BLOB_SIZE random bytes, MD5 them, INSERT.
//	WL4 blob-read   — SELECT data WHERE id = ?, MD5 each blob in app code.
//
// A start barrier (WaitGroup + closed channel) keeps connect/prepare out of the
// timed window; main samples a monotonic clock around it. Per-operation latency
// is collected in integer microseconds. If BENCH_PROGRESS_FILE is set, each
// worker publishes its completed-operation count into a per-worker slot it alone
// writes, and a monitor goroutine periodically sums the slots and rewrites the
// partial-result file atomically (tmp + rename).
package main

import (
	"context"
	"crypto/md5"
	"database/sql"
	"encoding/binary"
	"fmt"
	"os"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/go-sql-driver/mysql"
)

// Must match the version pinned in go.mod.
const driverVersion = "1.9.3"

// ---- configuration, set in main() before any worker starts ----
var (
	workload  int
	threads   int
	seed      int64
	intRows   int64
	intBatch  int64
	blobCount int64
	blobSize  int64

	progressFile string
	progressSec  int64
)

// workloadName / workloadUnit describe each workload for the JSON output.
var workloadName = map[int]string{1: "int-insert", 2: "int-fetch", 3: "blob-insert", 4: "blob-read"}
var workloadUnit = map[int]string{1: "rows/s", 2: "rows/s", 3: "blobs/s", 4: "blobs/s"}

// progress[i] is worker i's completed-operation count (rows for WL1/2, blobs
// for WL3/4). Worker i is the only writer; the monitor goroutine reads all.
var progress []int64

// globalSink keeps MD5 digests / summed values live so the compiler cannot
// elide the work that produced them.
var globalSink uint64

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

func envIntDefault(name string, def int64) int64 {
	if _, ok := os.LookupEnv(name); !ok {
		return def
	}
	return envInt(name)
}

// p(x) = arr[ceil(x/100 * n) - 1], 0-based, array sorted ascending.
func percentile(sorted []int64, x int) int64 {
	n := len(sorted)
	if n == 0 {
		return 0
	}
	idx := (x*n+99)/100 - 1
	if idx < 0 {
		idx = 0
	}
	return sorted[idx]
}

type worker struct {
	idx   int
	state uint64  // 31-bit LCG per SPEC.md, one independent stream per worker
	lat   []int64 // per-operation latency samples, integer microseconds
	rows  int64   // rows actually read (WL2) / inserted — for reported ops
	conn  *sql.Conn
}

// next advances the 31-bit LCG and returns the new state.
// state = (state*1103515245 + 12345) mod 2^31. (seed 42 worker 0 → 1250496027.)
func (w *worker) next() uint32 {
	w.state = (w.state*1103515245 + 12345) & 0x7fffffff
	return uint32(w.state)
}

func fail(idx int, err error) {
	fmt.Fprintf(os.Stderr, "worker %d error: %v\n", idx, err)
	os.Exit(1)
}

func (w *worker) prepare(ctx context.Context, query string) *sql.Stmt {
	st, err := w.conn.PrepareContext(ctx, query)
	if err != nil {
		fail(w.idx, err)
	}
	return st
}

// publishProgress stores the worker's completed-operation count in its slot.
func (w *worker) publishProgress(n int64) {
	if progressFile != "" {
		atomic.StoreInt64(&progress[w.idx], n)
	}
}

// ---- workload 1: integer insert ----
func (w *worker) runInsertInts(ctx context.Context) {
	batches := intRows / intBatch
	// Prepared multi-row INSERT with BENCH_INT_BATCH placeholders.
	var b strings.Builder
	b.WriteString("INSERT INTO bench_int (val) VALUES ")
	for i := int64(0); i < intBatch; i++ {
		if i > 0 {
			b.WriteByte(',')
		}
		b.WriteString("(?)")
	}
	st := w.prepare(ctx, b.String())

	w.lat = make([]int64, batches)
	args := make([]any, intBatch)

	// Autocommit ON: each multi-row INSERT is its own commit (no wrapping
	// transaction) — identical commit strategy across all languages per SPEC.
	var doneRows int64
	for bi := int64(0); bi < batches; bi++ {
		for j := int64(0); j < intBatch; j++ {
			args[j] = int32(w.next())
		}
		t0 := time.Now()
		if _, err := st.ExecContext(ctx, args...); err != nil {
			fail(w.idx, err)
		}
		w.lat[bi] = time.Since(t0).Microseconds()
		doneRows += intBatch
		w.publishProgress(doneRows)
	}
	w.rows = batches * intBatch
}

// ---- workload 2: integer fetch + sum ----
func (w *worker) runFetchInts(ctx context.Context) {
	st := w.prepare(ctx, "SELECT val FROM bench_int LIMIT ?")
	w.lat = make([]int64, 1)

	t0 := time.Now()
	rows, err := st.QueryContext(ctx, intRows)
	if err != nil {
		fail(w.idx, err)
	}
	var sum int64
	var val int64
	var n int64
	for rows.Next() {
		if err := rows.Scan(&val); err != nil {
			rows.Close()
			fail(w.idx, err)
		}
		sum += val
		n++
		if progressFile != "" && n%4096 == 0 {
			w.publishProgress(n)
		}
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		fail(w.idx, err)
	}
	if err := rows.Close(); err != nil {
		fail(w.idx, err)
	}
	w.lat[0] = time.Since(t0).Microseconds()
	w.rows = n
	w.publishProgress(n)
	atomic.AddUint64(&globalSink, uint64(sum))
}

// ---- workload 3: blob insert + MD5 ----
func (w *worker) runInsertBlobs(ctx context.Context) {
	st := w.prepare(ctx, "INSERT INTO bench_blob (id, data) VALUES (?, ?)")
	w.lat = make([]int64, blobCount)

	// Fill the buffer ONCE per worker with a cheap bulk LCG fill (8 bytes per
	// LCG step, compiled — not a per-byte interpreted loop). Each blob then
	// overwrites the first 8 bytes with its id so every blob (and MD5) differs.
	buf := make([]byte, blobSize)
	for j := int64(0); j+8 <= blobSize; j += 8 {
		// Advance the 31-bit LCG once, mix up to 64 bits, write 8 bytes.
		x := uint64(w.next()) * 0x2545f4914f6cdd1d
		binary.LittleEndian.PutUint64(buf[j:], x)
	}
	if rem := blobSize % 8; rem != 0 { // tail when size isn't a multiple of 8
		x := uint64(w.next()) * 0x2545f4914f6cdd1d
		for j := blobSize - rem; j < blobSize; j++ {
			buf[j] = byte(x)
			x >>= 8
		}
	}

	var sink uint64
	for k := int64(0); k < blobCount; k++ {
		id := int64(w.idx)*blobCount + k
		binary.LittleEndian.PutUint64(buf[0:8], uint64(id))
		digest := md5.Sum(buf)
		for _, b := range digest {
			sink ^= uint64(b)
		}
		t0 := time.Now()
		if _, err := st.ExecContext(ctx, id, buf); err != nil {
			fail(w.idx, err)
		}
		w.lat[k] = time.Since(t0).Microseconds()
		w.publishProgress(k + 1)
	}
	w.rows = blobCount
	atomic.AddUint64(&globalSink, sink)
}

// ---- workload 4: blob read + MD5 ----
func (w *worker) runReadBlobs(ctx context.Context) {
	st := w.prepare(ctx, "SELECT data FROM bench_blob WHERE id = ?")
	w.lat = make([]int64, blobCount)
	var sink uint64
	for k := int64(0); k < blobCount; k++ {
		id := int64(w.idx)*blobCount + k
		t0 := time.Now()
		rows, err := st.QueryContext(ctx, id)
		if err != nil {
			fail(w.idx, err)
		}
		var data []byte
		for rows.Next() {
			if err := rows.Scan(&data); err != nil {
				rows.Close()
				fail(w.idx, err)
			}
			digest := md5.Sum(data)
			for _, b := range digest {
				sink ^= uint64(b)
			}
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			fail(w.idx, err)
		}
		if err := rows.Close(); err != nil {
			fail(w.idx, err)
		}
		w.lat[k] = time.Since(t0).Microseconds()
		w.publishProgress(k + 1)
	}
	w.rows = blobCount
	atomic.AddUint64(&globalSink, sink)
}

func (w *worker) run(ctx context.Context, db *sql.DB, ready, done *sync.WaitGroup, start <-chan struct{}) {
	var err error
	w.conn, err = db.Conn(ctx) // dedicated session for this worker
	if err != nil {
		fail(w.idx, err)
	}

	ready.Done()
	<-start // released together with main's t_start sample

	switch workload {
	case 1:
		w.runInsertInts(ctx)
	case 2:
		w.runFetchInts(ctx)
	case 3:
		w.runInsertBlobs(ctx)
	case 4:
		w.runReadBlobs(ctx)
	}

	w.conn.Close()
	done.Done()
}

// writeProgress atomically overwrites progressFile with the partial result.
func writeProgress(ops int64, elapsed float64) {
	thr := 0.0
	if elapsed > 0 {
		thr = float64(ops) / elapsed
	}
	line := fmt.Sprintf(
		`{"workload":%d,"workload_name":"%s","unit":"%s","threads":%d,"ops":%d,"elapsed_s":%.3f,"throughput":%.1f,"partial":true}`+"\n",
		workload, workloadName[workload], workloadUnit[workload], threads, ops, elapsed, thr)
	tmp := progressFile + ".tmp"
	if err := os.WriteFile(tmp, []byte(line), 0o644); err != nil {
		return // progress is best-effort; never fail the benchmark over it
	}
	os.Rename(tmp, progressFile)
}

func main() {
	host := envStr("MYSQL_HOST")
	port := envStr("MYSQL_PORT")
	user := envStr("MYSQL_USER")
	password := envStr("MYSQL_PASSWORD")
	dbName := envStr("MYSQL_DB")

	workload = int(envInt("BENCH_WORKLOAD"))
	threads = int(envInt("BENCH_THREADS"))
	seed = envInt("BENCH_SEED")
	intRows = envIntDefault("BENCH_INT_ROWS", 1000000)
	intBatch = envIntDefault("BENCH_INT_BATCH", 10)
	blobCount = envIntDefault("BENCH_BLOB_COUNT", 100)
	blobSize = envIntDefault("BENCH_BLOB_SIZE", 1048576)

	progressFile = os.Getenv("BENCH_PROGRESS_FILE")
	progressSec = envIntDefault("BENCH_PROGRESS_SEC", 5)

	if workload < 1 || workload > 4 {
		fmt.Fprintf(os.Stderr, "invalid BENCH_WORKLOAD %d: must be 1..4\n", workload)
		os.Exit(1)
	}
	if threads <= 0 || intRows <= 0 || intBatch <= 0 || blobCount <= 0 || blobSize <= 0 || progressSec <= 0 {
		fmt.Fprintln(os.Stderr, "invalid workload configuration: BENCH_THREADS, BENCH_INT_ROWS, BENCH_INT_BATCH, BENCH_BLOB_COUNT, BENCH_BLOB_SIZE, BENCH_PROGRESS_SEC must be > 0")
		os.Exit(1)
	}
	if (workload == 1) && intRows%intBatch != 0 {
		fmt.Fprintf(os.Stderr, "BENCH_INT_ROWS (%d) must be a multiple of BENCH_INT_BATCH (%d)\n", intRows, intBatch)
		os.Exit(1)
	}
	if (workload == 3 || workload == 4) && blobSize < 8 {
		fmt.Fprintf(os.Stderr, "BENCH_BLOB_SIZE (%d) must be >= 8\n", blobSize)
		os.Exit(1)
	}

	// TCP, TLS disabled, server-side prepared statements.
	cfg := mysql.NewConfig()
	cfg.Net = "tcp"
	cfg.Addr = host + ":" + port
	cfg.User = user
	cfg.Passwd = password
	cfg.DBName = dbName
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

	progress = make([]int64, threads)
	workers := make([]*worker, threads)
	for i := 0; i < threads; i++ {
		workers[i] = &worker{idx: i, state: uint64(seed + int64(i))}
		go workers[i].run(ctx, pool, &ready, &done, start)
	}

	ready.Wait() // all workers connected and prepared
	tStart := time.Now()
	close(start)

	// Progress monitor: sum the per-worker slots on a timer and rewrite the
	// partial-result file atomically until the workers finish.
	var monitorDone chan struct{}
	if progressFile != "" {
		monitorDone = make(chan struct{})
		go func() {
			ticker := time.NewTicker(time.Duration(progressSec) * time.Second)
			defer ticker.Stop()
			for {
				select {
				case <-monitorDone:
					return
				case <-ticker.C:
					var ops int64
					for i := range progress {
						ops += atomic.LoadInt64(&progress[i])
					}
					writeProgress(ops, time.Since(tStart).Seconds())
				}
			}
		}()
	}

	done.Wait()
	elapsed := time.Since(tStart)
	if progressFile != "" {
		close(monitorDone)
	}

	// Merge latency samples, sort, compute percentiles.
	total := 0
	var ops int64
	for _, w := range workers {
		total += len(w.lat)
		ops += w.rows
	}
	all := make([]int64, 0, total)
	for _, w := range workers {
		all = append(all, w.lat...)
	}
	sort.Slice(all, func(i, j int) bool { return all[i] < all[j] })

	elapsedS := elapsed.Seconds()
	throughput := 0.0
	if elapsedS > 0 {
		throughput = float64(ops) / elapsedS
	}
	mbPerS := 0.0
	if workload == 3 || workload == 4 {
		if elapsedS > 0 {
			mbPerS = float64(ops) * float64(blobSize) / 1e6 / elapsedS
		}
	}

	clientHost, err := os.Hostname()
	if err != nil || clientHost == "" {
		clientHost = "unknown"
	}

	fmt.Fprintf(os.Stdout,
		`{"lang":"go","workload":%d,"workload_name":"%s","unit":"%s","driver":"go-sql-driver/mysql","driver_version":"%s","runtime_version":"%s","prepared":true,"client_host":"%s","db_host":"%s","threads":%d,"ops":%d,"errors":0,"elapsed_s":%.3f,"throughput":%.1f,"mb_per_s":%.1f,"p50_us":%d,"p95_us":%d,"p99_us":%d,"max_us":%d,"client_cpu_s":0,"server_cpu_s":0}`+"\n",
		workload, workloadName[workload], workloadUnit[workload],
		driverVersion, runtime.Version(), clientHost, host,
		threads, ops, elapsedS, throughput, mbPerS,
		percentile(all, 50), percentile(all, 95), percentile(all, 99), percentile(all, 100))

	// Success: the partial-result file is superseded by the stdout JSON.
	if progressFile != "" {
		os.Remove(progressFile)
	}
	_ = atomic.LoadUint64(&globalSink)
}
