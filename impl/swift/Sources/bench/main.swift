/*
 * mysql-lang-bench — Swift implementation (v3, four micro-workloads).
 *
 * Same driver as the C/C++ implementations: the libmysqlclient C API (via the
 * CMySQL system-library module) with mysql_stmt_* server-side prepared
 * statements, and the same API usage — MYSQL_BIND arrays bound once per
 * statement with value/result buffers at stable heap addresses, fetch loops to
 * MYSQL_NO_DATA, SSL_MODE_DISABLED + TCP + GET_SERVER_PUBLIC_KEY. MD5 is
 * computed with OpenSSL's MD5() from libcrypto (the CCrypto module).
 *
 * BENCH_WORKLOAD (1..4) selects one of:
 *   1 int-insert   prepared multi-row INSERT of BENCH_INT_BATCH rows,
 *                  BENCH_INT_ROWS/BENCH_INT_BATCH times, committed once.
 *   2 int-read     SELECT val ... LIMIT BENCH_INT_ROWS, sum in app code.
 *   3 blob-insert  one BENCH_BLOB_SIZE buffer filled once per worker with a
 *                  fast bulk LCG fill; before each of BENCH_BLOB_COUNT blobs
 *                  its first 8 bytes are overwritten with the id, then MD5 the
 *                  whole buffer and INSERT (id, data). Autocommit per INSERT.
 *   4 blob-read    read own BENCH_BLOB_COUNT blobs by id, MD5 each.
 *
 * BENCH_THREADS workers, each a Foundation Thread with its own MYSQL*, its own
 * prepared statement(s) and its own PRNG stream. Workers connect + prepare,
 * meet at a pthread_barrier; main releases it, samples a monotonic clock
 * (t_start), waits for every worker, samples t_end. Connection/prepare are
 * outside the timed window. One JSON line on stdout; any SQL/connection error
 * goes to stderr and exits non-zero (no retries — these workloads have no
 * concurrency conflicts).
 *
 * Intermediate progress: when BENCH_PROGRESS_FILE is set, each worker publishes
 * its own completed-operation count to a per-worker Int64 slot it alone writes
 * (no lock); a monitor Thread sums the slots every BENCH_PROGRESS_SEC seconds
 * and atomically overwrites the file (write .tmp, rename over it) with a
 * partial JSON line. On success the file is deleted.
 *
 * All MYSQL_BIND arrays and the value/result buffers they point at are manually
 * heap-allocated and stay at stable addresses for the statement's lifetime —
 * never Swift arrays or stack storage, which could move or be copied.
 */

import CCrypto
import CMySQL
import Dispatch
import Foundation
import Glibc

/* Pointer spellings of the imported libmysqlclient handle types. */
typealias MySQLConn = UnsafeMutablePointer<MYSQL>
typealias MySQLStmt = UnsafeMutablePointer<MYSQL_STMT>

/* runtime_version: Swift has no runtime API for its own version, so derive it
   from the compiler that built the binary (bench hosts pin the toolchain,
   currently Swift 6.2 in ~/swift). */
#if compiler(>=6.3)
let runtimeVersion = "swift 6.3"
#elseif compiler(>=6.2)
let runtimeVersion = "swift 6.2"
#elseif compiler(>=6.1)
let runtimeVersion = "swift 6.1"
#elseif compiler(>=6.0)
let runtimeVersion = "swift 6.0"
#else
let runtimeVersion = "swift 5.x"
#endif

/* ---------------- error helpers ------------------------------------------ */

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write(Data((msg + "\n").utf8))
    exit(1)
}

func dieMysql(_ conn: MySQLConn, _ what: String) -> Never {
    fail("\(what) failed: (\(mysql_errno(conn))) \(String(cString: mysql_error(conn)))")
}

func dieStmt(_ st: MySQLStmt, _ what: String) -> Never {
    fail("\(what) failed: (\(mysql_stmt_errno(st))) \(String(cString: mysql_stmt_error(st)))")
}

/* ---------------- env parsing -------------------------------------------- */

func envStr(_ name: String) -> String {
    guard let v = ProcessInfo.processInfo.environment[name] else {
        fail("missing required environment variable \(name)")
    }
    return v
}

func envInt(_ name: String) -> Int {
    let v = envStr(name)
    guard let n = Int(v) else {
        fail("environment variable \(name) is not an integer: '\(v)'")
    }
    return n
}

func envIntDefault(_ name: String, _ def: Int) -> Int {
    guard let v = ProcessInfo.processInfo.environment[name], !v.isEmpty else {
        return def
    }
    guard let n = Int(v) else {
        fail("environment variable \(name) is not an integer: '\(v)'")
    }
    return n
}

func envStrOptional(_ name: String) -> String? {
    guard let v = ProcessInfo.processInfo.environment[name], !v.isEmpty else {
        return nil
    }
    return v
}

/* ---------------- timing -------------------------------------------------- */

func nowNs() -> Int64 {
    var ts = timespec()
    clock_gettime(CLOCK_MONOTONIC, &ts)
    return Int64(ts.tv_sec) * 1_000_000_000 + Int64(ts.tv_nsec)
}

/* ---------------- stats --------------------------------------------------- */

/* p(x) = arr[ceil(x/100 * n) - 1], arr sorted ascending, 0-based index */
func percentile(_ arr: [Int64], _ x: Int) -> Int64 {
    if arr.isEmpty { return 0 }
    var idx = (x * arr.count + 99) / 100 - 1
    if idx < 0 { idx = 0 }
    if idx >= arr.count { idx = arr.count - 1 }
    return arr[idx]
}

/* ---------------- shared, read-only workload config ----------------------- */
/* Globals are assigned once in main() before any worker thread starts and are
   read-only afterwards (except the progress slots / stop flag, which use the
   publish-to-own-slot discipline described in the file header). */

var cfgHost = ""
var cfgUser = ""
var cfgPassword = ""
var cfgDb = ""
var cfgPort = 0

var workload = 0
var nThreads = 0
var baseSeed = 0
var intRows = 0
var intBatch = 0
var blobCount = 0
var blobSize = 0

/* workload metadata */
func workloadName(_ w: Int) -> String {
    switch w {
    case 1: return "int-insert"
    case 2: return "int-read"
    case 3: return "blob-insert"
    case 4: return "blob-read"
    default: return "unknown"
    }
}
func workloadUnit(_ w: Int) -> String {
    return (w == 3 || w == 4) ? "blobs/s" : "rows/s"
}

/* Start barrier (workers + main), heap-allocated: a stable address every thread
   can use without Swift inout-exclusivity conflicts. */
var startBarrier: UnsafeMutablePointer<pthread_barrier_t>! = nil

/* Each worker signals once when fully done; main waits nThreads times
   (Foundation Thread has no join). */
let doneSem = DispatchSemaphore(value: 0)

/* t_start, published once by main after the barrier releases, read by the
   monitor thread. The monitor is created only after this is set. */
var tStartNs: Int64 = 0

/* Per-worker completed-operation counters (rows for WL1/2, blobs for WL3/4).
   One Int64 slot per worker; worker i writes only slots[i], the monitor reads
   all. Stale reads are acceptable per SPEC. */
var progressSlots: UnsafeMutablePointer<Int64>! = nil

/* Monitor stop flag + completion handshake. */
var progressStop: UnsafeMutablePointer<Int32>! = nil
let monitorDone = DispatchSemaphore(value: 0)

/* Black-box sink: MD5/sum results are folded in so the compiler cannot elide
   the hashing/summing. Combined across workers and referenced after the run. */
var globalSink: UInt64 = 0

/* ---------------- PRNG: 31-bit LCG, identical in every language ----------- */

final class Worker {
    let index: Int
    var prngState: Int64

    var conn: MySQLConn! = nil

    /* Per-worker latency storage, pre-sized to the exact sample count (the
       counts are fixed per workload — no growth needed). */
    let lat: UnsafeMutablePointer<Int64>
    let latCap: Int
    var latCount: Int = 0

    /* Completed-op count for the final `ops` tally (also mirrored live into the
       worker's progress slot during the run). */
    var opsDone: Int64 = 0

    /* Per-worker black-box sink (folded into globalSink after the run). */
    var sink: UInt64 = 0

    init(index: Int, seed: Int, latCapacity: Int) {
        self.index = index
        self.prngState = Int64(seed)
        self.latCap = max(latCapacity, 1)
        lat = UnsafeMutablePointer<Int64>.allocate(capacity: self.latCap)
        lat.initialize(repeating: 0, count: self.latCap)
    }

    @inline(__always)
    func record(_ us: Int64) {
        if latCount < latCap {
            lat[latCount] = us
            latCount += 1
        }
    }

    func cleanup() {
        lat.deallocate()
    }
}

@inline(__always)
func prngNext(_ w: Worker) -> Int64 {
    /* (state * 1103515245 + 12345) mod 2^31; product < 2^62, no overflow —
       &* / &+ defensively (Swift would trap on plain overflow). */
    w.prngState = (w.prngState &* 1103515245 &+ 12345) & 0x7fffffff
    return w.prngState
}

/* ---------------- MD5 helper --------------------------------------------- */

/* Fold the 32 lowercase hex characters of a 16-byte MD5 digest into a sink so
   the hex formatting (required by SPEC) is actually computed, not elided. */
let hexDigits: [UInt8] = Array("0123456789abcdef".utf8)

@inline(__always)
func foldHexDigest(_ md: UnsafePointer<UInt8>, _ sink: inout UInt64) {
    for i in 0..<16 {
        let b = md[i]
        sink = sink &* 31 &+ UInt64(hexDigits[Int(b >> 4)])
        sink = sink &* 31 &+ UInt64(hexDigits[Int(b & 0x0f)])
    }
}

/* Fast bulk pseudo-random fill: a 64-bit LCG (SplitMix64-style constants)
   writing 8 bytes per step as native UInt64 words — NOT a per-byte interpreted
   loop (SPEC §Workload 3). Done once per worker; the content need not match
   other languages. `raw` is 8-byte aligned, so the UInt64 stores are aligned. */
func fillBlobBuffer(_ raw: UnsafeMutableRawPointer, _ size: Int, seed: UInt64) {
    var s = seed &+ 0x9E37_79B9_7F4A_7C15
    let words = size / 8
    for i in 0..<words {
        s = s &* 6_364_136_223_846_793_005 &+ 1_442_695_040_888_963_407
        raw.storeBytes(of: s, toByteOffset: i * 8, as: UInt64.self)
    }
    /* Tail bytes when size is not a multiple of 8 (default 1 MiB is). */
    let bytes = raw.assumingMemoryBound(to: UInt8.self)
    var j = words * 8
    var t = s
    while j < size {
        bytes[j] = UInt8(truncatingIfNeeded: t)
        t >>= 8
        j += 1
    }
}

/* ---------------- per-worker setup ----------------------------------------- */

func prepareStmt(_ conn: MySQLConn, _ sql: String) -> MySQLStmt {
    guard let stmt = mysql_stmt_init(conn) else {
        dieMysql(conn, "mysql_stmt_init")
    }
    if mysql_stmt_prepare(stmt, sql, UInt(sql.utf8.count)) != 0 {
        dieStmt(stmt, "mysql_stmt_prepare")
    }
    return stmt
}

func allocBind(_ n: Int) -> UnsafeMutablePointer<MYSQL_BIND> {
    let p = UnsafeMutablePointer<MYSQL_BIND>.allocate(capacity: n)
    p.initialize(repeating: MYSQL_BIND(), count: n) /* zeroed, like memset */
    return p
}

func workerConnect(_ w: Worker) {
    guard let conn = mysql_init(nil) else {
        fail("mysql_init failed (out of memory)")
    }
    w.conn = conn

    let proto = UInt32(MYSQL_PROTOCOL_TCP.rawValue)
    _ = withUnsafePointer(to: proto) {
        mysql_options(conn, MYSQL_OPT_PROTOCOL, $0)
    }
    /* Plaintext everywhere: libmysqlclient defaults to SSL_MODE_PREFERRED,
       which would put this driver on TLS while others run plaintext. */
    let sslMode = UInt32(SSL_MODE_DISABLED.rawValue)
    _ = withUnsafePointer(to: sslMode) {
        mysql_options(conn, MYSQL_OPT_SSL_MODE, $0)
    }
    /* caching_sha2_password full auth over plaintext needs the server's RSA key. */
    let getServerPK = true
    _ = withUnsafePointer(to: getServerPK) {
        mysql_options(conn, MYSQL_OPT_GET_SERVER_PUBLIC_KEY, $0)
    }

    if mysql_real_connect(conn, cfgHost, cfgUser, cfgPassword, cfgDb,
                          UInt32(cfgPort), nil, 0) == nil {
        dieMysql(conn, "mysql_real_connect")
    }
    /* Autocommit ON (SPEC): WL1/WL3 commit per INSERT, no wrapping transaction. */
    if mysql_autocommit(conn, true) {
        dieMysql(conn, "mysql_autocommit")
    }
}

/* ---------------- workload 1: integer insert ------------------------------- */

func runWorkload1(_ w: Worker) {
    let iterations = intRows / intBatch

    /* "INSERT INTO bench_int (val) VALUES (?),(?),…,(?)" with BENCH_INT_BATCH
       placeholders. */
    var sql = "INSERT INTO bench_int (val) VALUES "
    for j in 0..<intBatch { sql += (j == 0 ? "(?)" : ",(?)") }
    let stmt = prepareStmt(w.conn, sql)

    /* One signed-32-bit value buffer per placeholder, bound once. */
    let vals = UnsafeMutablePointer<Int32>.allocate(capacity: intBatch)
    vals.initialize(repeating: 0, count: intBatch)
    let binds = allocBind(intBatch)
    for j in 0..<intBatch {
        binds[j].buffer_type = MYSQL_TYPE_LONG
        binds[j].buffer = UnsafeMutableRawPointer(vals + j)
        binds[j].is_unsigned = false /* signed 32-bit */
    }
    if mysql_stmt_bind_param(stmt, binds) {
        dieStmt(stmt, "mysql_stmt_bind_param(int-insert)")
    }

    _ = pthread_barrier_wait(startBarrier)

    /* Autocommit ON: each multi-row INSERT is its own commit — NO wrapping
       transaction (SPEC; the commit strategy must be identical in every
       language, and the server does not fsync per commit). */
    for _ in 0..<iterations {
        for j in 0..<intBatch {
            vals[j] = Int32(truncatingIfNeeded: prngNext(w))
        }
        let t0 = nowNs()
        if mysql_stmt_execute(stmt) != 0 {
            dieStmt(stmt, "int-insert execute")
        }
        w.record((nowNs() - t0) / 1000)
        w.opsDone += Int64(intBatch)
        progressSlots[w.index] = w.opsDone
    }

    _ = mysql_stmt_close(stmt)
    binds.deinitialize(count: intBatch); binds.deallocate()
    vals.deallocate()
}

/* ---------------- workload 2: integer fetch + sum -------------------------- */

func runWorkload2(_ w: Worker) {
    let stmt = prepareStmt(w.conn, "SELECT val FROM bench_int LIMIT \(intRows)")

    let valBuf = UnsafeMutablePointer<Int32>.allocate(capacity: 1)
    valBuf.initialize(to: 0)
    let binds = allocBind(1)
    binds[0].buffer_type = MYSQL_TYPE_LONG
    binds[0].buffer = UnsafeMutableRawPointer(valBuf)
    binds[0].is_unsigned = false
    if mysql_stmt_bind_result(stmt, binds) {
        dieStmt(stmt, "mysql_stmt_bind_result(int-read)")
    }

    _ = pthread_barrier_wait(startBarrier)

    /* One latency sample per worker for the whole scan. */
    let t0 = nowNs()
    if mysql_stmt_execute(stmt) != 0 {
        dieStmt(stmt, "int-read execute")
    }
    var sum: Int64 = 0
    var rows: Int64 = 0
    while true {
        let rc = mysql_stmt_fetch(stmt)
        if rc == 0 || rc == MYSQL_DATA_TRUNCATED {
            sum = sum &+ Int64(valBuf.pointee)
            rows += 1
            progressSlots[w.index] = rows
            continue
        }
        if rc == MYSQL_NO_DATA { break }
        dieStmt(stmt, "int-read fetch")
    }
    w.record((nowNs() - t0) / 1000)
    w.opsDone = rows
    w.sink = UInt64(bitPattern: sum)

    _ = mysql_stmt_close(stmt)
    binds.deinitialize(count: 1); binds.deallocate()
    valBuf.deallocate()
}

/* ---------------- workload 3: blob insert + MD5 ---------------------------- */

func runWorkload3(_ w: Worker) {
    let stmt = prepareStmt(w.conn,
                           "INSERT INTO bench_blob (id, data) VALUES (?, ?)")

    let idBuf = UnsafeMutablePointer<Int64>.allocate(capacity: 1)
    idBuf.initialize(to: 0)
    /* 8-byte aligned so the bulk UInt64 fill and the id overwrite are aligned. */
    let blobRaw = UnsafeMutableRawPointer.allocate(byteCount: blobSize, alignment: 8)
    let blobBuf = blobRaw.bindMemory(to: UInt8.self, capacity: blobSize)
    let blobLen = UnsafeMutablePointer<UInt>.allocate(capacity: 1)
    blobLen.initialize(to: UInt(blobSize))
    let md = UnsafeMutablePointer<UInt8>.allocate(capacity: 16)
    md.initialize(repeating: 0, count: 16)

    let binds = allocBind(2)
    binds[0].buffer_type = MYSQL_TYPE_LONGLONG
    binds[0].buffer = UnsafeMutableRawPointer(idBuf)
    binds[0].is_unsigned = false
    binds[1].buffer_type = MYSQL_TYPE_LONG_BLOB
    binds[1].buffer = blobRaw
    binds[1].buffer_length = UInt(blobSize)
    binds[1].length = blobLen
    if mysql_stmt_bind_param(stmt, binds) {
        dieStmt(stmt, "mysql_stmt_bind_param(blob-insert)")
    }

    /* Produce the buffer ONCE per worker with a fast bulk fill (SPEC: not a
       per-byte loop). Before each blob the first 8 bytes are overwritten with
       the blob id, so every blob's content — and its MD5 — differs. */
    fillBlobBuffer(blobRaw, blobSize,
                   seed: UInt64(bitPattern: Int64(baseSeed + w.index)))

    _ = pthread_barrier_wait(startBarrier)

    var sink: UInt64 = 0
    for k in 0..<blobCount {
        let idv = Int64(w.index * blobCount + k)
        /* Overwrite first 8 bytes with the id → unique content per blob. */
        blobRaw.storeBytes(of: idv, toByteOffset: 0, as: Int64.self)
        /* MD5 of the whole BENCH_BLOB_SIZE buffer; hex digest into the sink. */
        _ = MD5(blobBuf, blobSize, md)
        foldHexDigest(md, &sink)

        idBuf.pointee = idv
        let t0 = nowNs()
        if mysql_stmt_execute(stmt) != 0 {
            dieStmt(stmt, "blob-insert execute")
        }
        w.record((nowNs() - t0) / 1000)
        w.opsDone += 1
        progressSlots[w.index] = w.opsDone
    }
    w.sink = sink

    _ = mysql_stmt_close(stmt)
    binds.deinitialize(count: 2); binds.deallocate()
    idBuf.deallocate(); blobRaw.deallocate(); blobLen.deallocate(); md.deallocate()
}

/* ---------------- workload 4: blob read + MD5 ------------------------------ */

func runWorkload4(_ w: Worker) {
    let stmt = prepareStmt(w.conn, "SELECT data FROM bench_blob WHERE id = ?")

    let idBuf = UnsafeMutablePointer<Int64>.allocate(capacity: 1)
    idBuf.initialize(to: 0)
    let blobBuf = UnsafeMutablePointer<UInt8>.allocate(capacity: blobSize)
    blobBuf.initialize(repeating: 0, count: blobSize)
    let outLen = UnsafeMutablePointer<UInt>.allocate(capacity: 1)
    outLen.initialize(to: 0)
    let md = UnsafeMutablePointer<UInt8>.allocate(capacity: 16)
    md.initialize(repeating: 0, count: 16)

    let pbind = allocBind(1)
    pbind[0].buffer_type = MYSQL_TYPE_LONGLONG
    pbind[0].buffer = UnsafeMutableRawPointer(idBuf)
    pbind[0].is_unsigned = false
    if mysql_stmt_bind_param(stmt, pbind) {
        dieStmt(stmt, "mysql_stmt_bind_param(blob-read)")
    }
    let rbind = allocBind(1)
    rbind[0].buffer_type = MYSQL_TYPE_LONG_BLOB
    rbind[0].buffer = UnsafeMutableRawPointer(blobBuf)
    rbind[0].buffer_length = UInt(blobSize)
    rbind[0].length = outLen
    if mysql_stmt_bind_result(stmt, rbind) {
        dieStmt(stmt, "mysql_stmt_bind_result(blob-read)")
    }

    _ = pthread_barrier_wait(startBarrier)

    var sink: UInt64 = 0
    for k in 0..<blobCount {
        idBuf.pointee = Int64(w.index * blobCount + k)
        let t0 = nowNs()
        if mysql_stmt_execute(stmt) != 0 {
            dieStmt(stmt, "blob-read execute")
        }
        let rc = mysql_stmt_fetch(stmt)
        if rc == MYSQL_NO_DATA {
            fail("blob-read: id \(idBuf.pointee) not found")
        }
        if rc != 0 && rc != MYSQL_DATA_TRUNCATED {
            dieStmt(stmt, "blob-read fetch")
        }
        w.record((nowNs() - t0) / 1000)

        /* MD5 of the blob content in app code; hex digest consumed into sink. */
        _ = MD5(blobBuf, Int(outLen.pointee), md)
        foldHexDigest(md, &sink)

        /* A PK lookup returns exactly one row; drain the result set so the next
           execute does not hit "commands out of sync". */
        while true {
            let r = mysql_stmt_fetch(stmt)
            if r == MYSQL_NO_DATA { break }
            if r != 0 && r != MYSQL_DATA_TRUNCATED {
                dieStmt(stmt, "blob-read drain")
            }
        }

        w.opsDone += 1
        progressSlots[w.index] = w.opsDone
    }
    w.sink = sink

    _ = mysql_stmt_close(stmt)
    pbind.deinitialize(count: 1); pbind.deallocate()
    rbind.deinitialize(count: 1); rbind.deallocate()
    idBuf.deallocate(); blobBuf.deallocate(); outLen.deallocate(); md.deallocate()
}

/* ---------------- worker thread -------------------------------------------- */

func workerMain(_ w: Worker) {
    workerConnect(w)

    switch workload {
    case 1: runWorkload1(w)
    case 2: runWorkload2(w)
    case 3: runWorkload3(w)
    case 4: runWorkload4(w)
    default: fail("invalid BENCH_WORKLOAD \(workload) (expected 1..4)")
    }

    mysql_close(w.conn)
    mysql_thread_end()
    doneSem.signal()
}

/* ---------------- progress monitor ----------------------------------------- */

var progressPath: String? = nil
var progressTmpPath: String = ""
var progressSec = 5

func writeProgress() {
    guard let path = progressPath else { return }
    var ops: Int64 = 0
    for i in 0..<nThreads { ops += progressSlots[i] }
    let elapsed = Double(nowNs() - tStartNs) / 1e9
    let thr = elapsed > 0 ? Double(ops) / elapsed : 0.0

    var json = "{\"workload\":\(workload)"
    json += ",\"workload_name\":\"\(workloadName(workload))\""
    json += ",\"unit\":\"\(workloadUnit(workload))\""
    json += ",\"threads\":\(nThreads)"
    json += ",\"ops\":\(ops)"
    json += ",\"elapsed_s\":\(String(format: "%.3f", elapsed))"
    json += ",\"throughput\":\(String(format: "%.1f", thr))"
    json += ",\"partial\":true}\n"

    /* Atomic overwrite: write FILE.tmp, then rename over FILE. */
    guard let fp = fopen(progressTmpPath, "w") else { return }
    _ = json.withCString { fputs($0, fp) }
    fclose(fp)
    _ = progressTmpPath.withCString { tmpC in
        path.withCString { dstC in rename(tmpC, dstC) }
    }
}

func monitorMain() {
    /* Poll the stop flag in short steps so shutdown is prompt, writing roughly
       every progressSec seconds. */
    let stepUs: useconds_t = 100_000 /* 100 ms */
    let stepsPerInterval = max(1, progressSec * 10)
    while progressStop.pointee == 0 {
        var steps = 0
        while steps < stepsPerInterval && progressStop.pointee == 0 {
            usleep(stepUs)
            steps += 1
        }
        if progressStop.pointee != 0 { break }
        writeProgress()
    }
    monitorDone.signal()
}

/* ---------------- main ----------------------------------------------------- */

func main() {
    /* Connection config */
    cfgHost = envStr("MYSQL_HOST")
    cfgPort = envInt("MYSQL_PORT")
    cfgUser = envStr("MYSQL_USER")
    cfgPassword = envStr("MYSQL_PASSWORD")
    cfgDb = envStr("MYSQL_DB")

    /* Workload config */
    workload = envInt("BENCH_WORKLOAD")
    nThreads = envInt("BENCH_THREADS")
    baseSeed = envInt("BENCH_SEED")
    intRows = envIntDefault("BENCH_INT_ROWS", 1_000_000)
    intBatch = envIntDefault("BENCH_INT_BATCH", 10)
    blobCount = envIntDefault("BENCH_BLOB_COUNT", 100)
    blobSize = envIntDefault("BENCH_BLOB_SIZE", 1_048_576)

    if workload < 1 || workload > 4 { fail("BENCH_WORKLOAD must be 1..4") }
    if nThreads <= 0 { fail("BENCH_THREADS must be > 0") }
    if (workload == 1 || workload == 2) && intRows <= 0 {
        fail("BENCH_INT_ROWS must be > 0")
    }
    if workload == 1 && (intBatch <= 0 || intRows % intBatch != 0) {
        fail("BENCH_INT_BATCH must be > 0 and divide BENCH_INT_ROWS")
    }
    if (workload == 3 || workload == 4) && (blobCount <= 0 || blobSize <= 0) {
        fail("BENCH_BLOB_COUNT and BENCH_BLOB_SIZE must be > 0")
    }
    if workload == 3 && blobSize < 8 {
        fail("BENCH_BLOB_SIZE must be >= 8 (the blob id occupies the first 8 bytes)")
    }

    /* Progress config */
    progressPath = envStrOptional("BENCH_PROGRESS_FILE")
    progressSec = max(1, envIntDefault("BENCH_PROGRESS_SEC", 5))
    if let p = progressPath { progressTmpPath = p + ".tmp" }

    /* Must be called before any threads touch the client library.
       (mysql_library_init is a macro aliasing mysql_server_init.) */
    if mysql_server_init(0, nil, nil) != 0 {
        fail("mysql_library_init failed")
    }

    /* Latency samples per worker: WL1 = iterations, WL2 = 1, WL3/4 = blobCount. */
    let latCap: Int
    switch workload {
    case 1: latCap = intRows / intBatch
    case 2: latCap = 1
    default: latCap = blobCount
    }

    let workers = (0..<nThreads).map {
        Worker(index: $0, seed: baseSeed + $0, latCapacity: latCap)
    }

    progressSlots = UnsafeMutablePointer<Int64>.allocate(capacity: nThreads)
    progressSlots.initialize(repeating: 0, count: nThreads)
    progressStop = UnsafeMutablePointer<Int32>.allocate(capacity: 1)
    progressStop.initialize(to: 0)

    startBarrier = UnsafeMutablePointer<pthread_barrier_t>.allocate(capacity: 1)
    startBarrier.initialize(to: pthread_barrier_t())
    if pthread_barrier_init(startBarrier, nil, UInt32(nThreads + 1)) != 0 {
        fail("pthread_barrier_init failed")
    }

    for w in workers {
        let t = Thread { workerMain(w) }
        t.start()
    }

    /* Workers connect and prepare, then block on the barrier. When the last one
       arrives, everybody is released: sample t_start. */
    _ = pthread_barrier_wait(startBarrier)
    tStartNs = nowNs()

    /* Launch the progress monitor only after t_start is known. */
    if progressPath != nil {
        let m = Thread { monitorMain() }
        m.start()
    }

    for _ in 0..<nThreads {
        doneSem.wait()
    }
    let tEnd = nowNs()

    /* Stop the monitor and wait for it to exit (no final write). */
    if progressPath != nil {
        progressStop.pointee = 1
        monitorDone.wait()
    }

    _ = pthread_barrier_destroy(startBarrier)

    let driverVersion = String(cString: mysql_get_client_info())

    /* Merge latency samples, sort, percentiles. */
    var n = 0
    for w in workers { n += w.latCount }
    var lat = [Int64](repeating: 0, count: n)
    var idx = 0
    for w in workers {
        for i in 0..<w.latCount {
            lat[idx] = w.lat[i]
            idx += 1
        }
        globalSink = globalSink &+ w.sink
    }
    lat.sort()
    let p50 = percentile(lat, 50)
    let p95 = percentile(lat, 95)
    let p99 = percentile(lat, 99)
    let maxUs = n > 0 ? lat[n - 1] : 0

    let elapsedS = Double(tEnd - tStartNs) / 1e9

    /* ops per SPEC: WL1 = threads*INT_ROWS, WL3 = threads*BLOB_COUNT;
       WL2/WL4 = actual rows/blobs read (summed). */
    let ops: Int64
    switch workload {
    case 1: ops = Int64(nThreads) * Int64(intRows)
    case 3: ops = Int64(nThreads) * Int64(blobCount)
    default:
        var s: Int64 = 0
        for w in workers { s += w.opsDone }
        ops = s
    }

    let throughput = elapsedS > 0 ? Double(ops) / elapsedS : 0.0

    /* mb_per_s only for the blob workloads; int workloads report 0. */
    let mbStr: String
    if workload == 3 || workload == 4 {
        let mb = elapsedS > 0
            ? Double(ops) * Double(blobSize) / 1e6 / elapsedS : 0.0
        mbStr = String(format: "%.1f", mb)
    } else {
        mbStr = "0"
    }

    var hostBuf = [CChar](repeating: 0, count: 256)
    var clientHost = "unknown"
    if gethostname(&hostBuf, 255) == 0 {
        clientHost = hostBuf.withUnsafeBufferPointer { String(cString: $0.baseAddress!) }
    }

    /* One JSON line, SPEC field order. client_cpu_s / server_cpu_s are filled
       by the harness, so emit 0. */
    var json = "{\"lang\":\"swift\""
    json += ",\"workload\":\(workload)"
    json += ",\"workload_name\":\"\(workloadName(workload))\""
    json += ",\"unit\":\"\(workloadUnit(workload))\""
    json += ",\"driver\":\"libmysqlclient\""
    json += ",\"driver_version\":\"\(driverVersion)\""
    json += ",\"runtime_version\":\"\(runtimeVersion)\""
    json += ",\"prepared\":true"
    json += ",\"client_host\":\"\(clientHost)\""
    json += ",\"db_host\":\"\(cfgHost)\""
    json += ",\"threads\":\(nThreads)"
    json += ",\"ops\":\(ops)"
    json += ",\"errors\":0"
    json += ",\"elapsed_s\":\(String(format: "%.3f", elapsedS))"
    json += ",\"throughput\":\(String(format: "%.1f", throughput))"
    json += ",\"mb_per_s\":\(mbStr)"
    json += ",\"p50_us\":\(p50)"
    json += ",\"p95_us\":\(p95)"
    json += ",\"p99_us\":\(p99)"
    json += ",\"max_us\":\(maxUs)"
    json += ",\"client_cpu_s\":0,\"server_cpu_s\":0}"
    print(json)

    /* Supersede the partial progress file now that stdout has the final result. */
    if let p = progressPath {
        _ = p.withCString { unlink($0) }
    }

    /* Reference the sink so the MD5/sum work cannot be optimized away. */
    if globalSink == 0xFFFF_FFFF_FFFF_FFFF {
        FileHandle.standardError.write(Data("\(globalSink)".utf8))
    }

    for w in workers { w.cleanup() }
    startBarrier.deallocate()
    progressSlots.deallocate()
    progressStop.deallocate()
    mysql_server_end()
}

main()
