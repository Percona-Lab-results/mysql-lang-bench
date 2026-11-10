/*
 * mysql-lang-bench — Swift implementation (v2, multithreaded oltp_read_write).
 *
 * Same driver as the C/C++ implementations: the libmysqlclient C API (via the
 * CMySQL system-library module) with mysql_stmt_* server-side prepared
 * statements, and the same API usage — MYSQL_BIND arrays bound once per
 * statement, value buffers rewritten before each execute, fetch to
 * MYSQL_NO_DATA, SSL_MODE_DISABLED + TCP + GET_SERVER_PUBLIC_KEY.
 *
 * See SPEC.md: BENCH_THREADS workers (one OS thread each, Foundation Thread),
 * each with its own connection, its own 18 prepared statements and its own
 * PRNG stream, run a sysbench-oltp_read_write-style transaction loop.
 * One JSON line on stdout.
 *
 * All MYSQL_BIND arrays and the value/result buffers they point at are
 * manually allocated per worker and stay alive (at stable addresses) for the
 * worker's whole lifetime — never Swift arrays or stack storage, which could
 * move or be copied.
 */

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

/* Retryable concurrency errors per SPEC: deadlock, lock wait timeout,
   duplicate key on the insert. */
func errRetryable(_ e: UInt32) -> Bool {
    return e == 1213 || e == 1205 || e == 1062
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

/* ---------------- timing -------------------------------------------------- */

func nowNs() -> Int64 {
    var ts = timespec()
    clock_gettime(CLOCK_MONOTONIC, &ts)
    return Int64(ts.tv_sec) * 1_000_000_000 + Int64(ts.tv_nsec)
}

/* ---------------- stats --------------------------------------------------- */

/* p(x) = arr[ceil(x/100 * n) - 1], arr sorted ascending, 0-based index */
func percentile(_ arr: [Int64], _ x: Int) -> Int64 {
    var idx = (x * arr.count + 99) / 100 - 1
    if idx < 0 { idx = 0 }
    return arr[idx]
}

/* ---------------- shared, read-only workload config ----------------------- */
/* Globals are assigned once in main() before any worker thread starts and are
   read-only afterwards. */

var cfgHost = ""
var cfgUser = ""
var cfgPassword = ""
var cfgDb = ""
var cfgPort = 0
var tableRows = 0
var nThreads = 0
var warmupTxns = 0
var txnsPerThread = 0
var rangeSize = 0
var baseSeed = 0

/* Constant string parameters, read-only after init — safe to share across
   workers (as in the C implementation). Heap-allocated so the MYSQL_BIND
   buffer/length pointers are stable. */
var cValBuf: UnsafeMutablePointer<CChar>! = nil   /* "abcdefghij" x 12 */
var cValLen: UnsafeMutablePointer<UInt>! = nil    /* 120 */
var padValBuf: UnsafeMutablePointer<CChar>! = nil /* "0123456789" x 6 */
var padValLen: UnsafeMutablePointer<UInt>! = nil  /* 60 */

func makeCBuf(_ s: String) -> UnsafeMutablePointer<CChar> {
    let bytes = Array(s.utf8)
    let p = UnsafeMutablePointer<CChar>.allocate(capacity: bytes.count + 1)
    for (i, b) in bytes.enumerated() { p[i] = CChar(bitPattern: b) }
    p[bytes.count] = 0
    return p
}

/* Start barrier (workers + main), heap-allocated: a stable address every
   thread can use without Swift inout-exclusivity conflicts. */
var startBarrier: UnsafeMutablePointer<pthread_barrier_t>! = nil

/* Each worker signals once when fully done; main waits nThreads times
   (Foundation Thread has no join). */
let doneSem = DispatchSemaphore(value: 0)

/* ---------------- per-worker state ---------------------------------------- */

/* The 18 server-side prepared statements, one set per worker. */
let ST_POINT0 = 0 /* 10 point selects: ST_POINT0 .. ST_POINT0+9 */
let ST_RANGE_SIMPLE = 10
let ST_RANGE_SUM = 11
let ST_RANGE_ORDER = 12
let ST_RANGE_DISTINCT = 13
let ST_UPD_INDEX = 14
let ST_UPD_NONINDEX = 15
let ST_DELETE = 16
let ST_INSERT = 17
let ST_COUNT = 18

final class Worker {
    let index: Int
    var prngState: Int64
    var retries: Int64 = 0

    var conn: MySQLConn! = nil
    var st: [MySQLStmt?] = Array(repeating: nil, count: ST_COUNT)

    /* Parameter value buffers (bound once; values written before execute). */
    let pointId: UnsafeMutablePointer<UInt32>       /* [10] */
    let rangeLo: UnsafeMutablePointer<UInt32>       /* [4]: simple, sum, order, distinct */
    let rangeHi: UnsafeMutablePointer<UInt32>       /* [4] */
    let updIndexId: UnsafeMutablePointer<UInt32>
    let updNonindexId: UnsafeMutablePointer<UInt32>
    let delId: UnsafeMutablePointer<UInt32>
    let insId: UnsafeMutablePointer<UInt32>
    let insK: UnsafeMutablePointer<UInt32>

    /* Result buffers. One worker runs its statements strictly sequentially,
       so the selects returning column c can share one row buffer. */
    let rowC: UnsafeMutablePointer<CChar>           /* [512]; CHAR(120) */
    let rowCLen: UnsafeMutablePointer<UInt>
    let sumBuf: UnsafeMutablePointer<CChar>         /* [80]; SUM(k) as DECIMAL text */
    let sumLen: UnsafeMutablePointer<UInt>
    let sumIsNull: UnsafeMutablePointer<Bool>

    /* MYSQL_BIND arrays kept alive for the worker's whole lifetime. */
    let bpPoint: UnsafeMutablePointer<MYSQL_BIND>       /* [10] (1 param each) */
    let brPoint: UnsafeMutablePointer<MYSQL_BIND>       /* [10] (1 result each) */
    let bpRange: UnsafeMutablePointer<MYSQL_BIND>       /* [4][2] -> flat [8] */
    let brRangeC: UnsafeMutablePointer<MYSQL_BIND>      /* [3]: simple, order, distinct */
    let brSum: UnsafeMutablePointer<MYSQL_BIND>         /* [1] */
    let bpUpdIndex: UnsafeMutablePointer<MYSQL_BIND>    /* [1] */
    let bpUpdNonindex: UnsafeMutablePointer<MYSQL_BIND> /* [2] */
    let bpDel: UnsafeMutablePointer<MYSQL_BIND>         /* [1] */
    let bpIns: UnsafeMutablePointer<MYSQL_BIND>         /* [4] */

    let lat: UnsafeMutablePointer<Int64> /* [txnsPerThread], pre-allocated */

    init(index: Int, seed: Int, txns: Int) {
        self.index = index
        self.prngState = Int64(seed)

        func allocU32(_ n: Int) -> UnsafeMutablePointer<UInt32> {
            let p = UnsafeMutablePointer<UInt32>.allocate(capacity: n)
            p.initialize(repeating: 0, count: n)
            return p
        }
        func allocBind(_ n: Int) -> UnsafeMutablePointer<MYSQL_BIND> {
            let p = UnsafeMutablePointer<MYSQL_BIND>.allocate(capacity: n)
            p.initialize(repeating: MYSQL_BIND(), count: n) /* zeroed, like memset */
            return p
        }

        pointId = allocU32(10)
        rangeLo = allocU32(4)
        rangeHi = allocU32(4)
        updIndexId = allocU32(1)
        updNonindexId = allocU32(1)
        delId = allocU32(1)
        insId = allocU32(1)
        insK = allocU32(1)

        rowC = UnsafeMutablePointer<CChar>.allocate(capacity: 512)
        rowC.initialize(repeating: 0, count: 512)
        rowCLen = UnsafeMutablePointer<UInt>.allocate(capacity: 1)
        rowCLen.initialize(to: 0)
        sumBuf = UnsafeMutablePointer<CChar>.allocate(capacity: 80)
        sumBuf.initialize(repeating: 0, count: 80)
        sumLen = UnsafeMutablePointer<UInt>.allocate(capacity: 1)
        sumLen.initialize(to: 0)
        sumIsNull = UnsafeMutablePointer<Bool>.allocate(capacity: 1)
        sumIsNull.initialize(to: false)

        bpPoint = allocBind(10)
        brPoint = allocBind(10)
        bpRange = allocBind(8)
        brRangeC = allocBind(3)
        brSum = allocBind(1)
        bpUpdIndex = allocBind(1)
        bpUpdNonindex = allocBind(2)
        bpDel = allocBind(1)
        bpIns = allocBind(4)

        lat = UnsafeMutablePointer<Int64>.allocate(capacity: txns)
        lat.initialize(repeating: 0, count: txns)
    }

    /* Called from main() after the worker thread has finished (it closed the
       statements and the connection itself). */
    func cleanup() {
        pointId.deallocate(); rangeLo.deallocate(); rangeHi.deallocate()
        updIndexId.deallocate(); updNonindexId.deallocate()
        delId.deallocate(); insId.deallocate(); insK.deallocate()
        rowC.deallocate(); rowCLen.deallocate()
        sumBuf.deallocate(); sumLen.deallocate(); sumIsNull.deallocate()
        bpPoint.deallocate(); brPoint.deallocate(); bpRange.deallocate()
        brRangeC.deallocate(); brSum.deallocate(); bpUpdIndex.deallocate()
        bpUpdNonindex.deallocate(); bpDel.deallocate(); bpIns.deallocate()
        lat.deallocate()
    }
}

/* ---------------- PRNG: 31-bit LCG, identical in every language ----------- */

@inline(__always)
func prngNext(_ w: Worker) -> Int64 {
    /* (state * 1103515245 + 12345) mod 2^31; product < 2^62, no overflow —
       &* / &+ defensively (Swift would trap on plain overflow). */
    w.prngState = (w.prngState &* 1103515245 &+ 12345) & 0x7fffffff
    return w.prngState
}

@inline(__always)
func prngRand(_ w: Worker, _ n: Int) -> Int { /* uniform 0..n-1 */
    return Int(prngNext(w) % Int64(n))
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

func bindUIntParam(_ b: UnsafeMutablePointer<MYSQL_BIND>,
                   _ value: UnsafeMutablePointer<UInt32>) {
    b.pointee = MYSQL_BIND() /* zeroed */
    b.pointee.buffer_type = MYSQL_TYPE_LONG
    b.pointee.buffer = UnsafeMutableRawPointer(value)
    b.pointee.is_unsigned = true
}

func bindCResult(_ b: UnsafeMutablePointer<MYSQL_BIND>, _ w: Worker) {
    b.pointee = MYSQL_BIND() /* zeroed */
    b.pointee.buffer_type = MYSQL_TYPE_STRING
    b.pointee.buffer = UnsafeMutableRawPointer(w.rowC)
    b.pointee.buffer_length = 512
    b.pointee.length = w.rowCLen
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
    if mysql_autocommit(conn, true) {
        dieMysql(conn, "mysql_autocommit")
    }
}

func workerPrepare(_ w: Worker) {
    let conn: MySQLConn = w.conn

    /* 10 point selects, each its own prepared statement (sysbench-style). */
    for j in 0..<10 {
        let st = prepareStmt(conn, "SELECT c FROM sbtest WHERE id = ?")
        w.st[ST_POINT0 + j] = st
        bindUIntParam(w.bpPoint + j, w.pointId + j)
        if mysql_stmt_bind_param(st, w.bpPoint + j) {
            dieStmt(st, "mysql_stmt_bind_param(point)")
        }
        bindCResult(w.brPoint + j, w)
        if mysql_stmt_bind_result(st, w.brPoint + j) {
            dieStmt(st, "mysql_stmt_bind_result(point)")
        }
    }

    /* 4 range selects: params (lo, hi) each. */
    let rangeSql = [
        "SELECT c FROM sbtest WHERE id BETWEEN ? AND ?",
        "SELECT SUM(k) FROM sbtest WHERE id BETWEEN ? AND ?",
        "SELECT c FROM sbtest WHERE id BETWEEN ? AND ? ORDER BY c",
        "SELECT DISTINCT c FROM sbtest WHERE id BETWEEN ? AND ? ORDER BY c",
    ]
    for j in 0..<4 {
        let st = prepareStmt(conn, rangeSql[j])
        w.st[ST_RANGE_SIMPLE + j] = st
        bindUIntParam(w.bpRange + j * 2, w.rangeLo + j)
        bindUIntParam(w.bpRange + j * 2 + 1, w.rangeHi + j)
        if mysql_stmt_bind_param(st, w.bpRange + j * 2) {
            dieStmt(st, "mysql_stmt_bind_param(range)")
        }
    }

    /* Result binds: column c for simple/order/distinct (shared row buffer). */
    let cStmts = [ST_RANGE_SIMPLE, ST_RANGE_ORDER, ST_RANGE_DISTINCT]
    for j in 0..<3 {
        bindCResult(w.brRangeC + j, w)
        if mysql_stmt_bind_result(w.st[cStmts[j]]!, w.brRangeC + j) {
            dieStmt(w.st[cStmts[j]]!, "mysql_stmt_bind_result(range)")
        }
    }

    /* SUM(k) over INT UNSIGNED yields DECIMAL; fetch it as a string buffer.
       It is NULL when the range matches no rows (rows may be deleted by
       concurrent workers), so bind is_null too. */
    w.brSum.pointee = MYSQL_BIND()
    w.brSum.pointee.buffer_type = MYSQL_TYPE_NEWDECIMAL
    w.brSum.pointee.buffer = UnsafeMutableRawPointer(w.sumBuf)
    w.brSum.pointee.buffer_length = 80
    w.brSum.pointee.length = w.sumLen
    w.brSum.pointee.is_null = w.sumIsNull
    if mysql_stmt_bind_result(w.st[ST_RANGE_SUM]!, w.brSum) {
        dieStmt(w.st[ST_RANGE_SUM]!, "mysql_stmt_bind_result(sum)")
    }

    /* Index update. */
    w.st[ST_UPD_INDEX] = prepareStmt(conn, "UPDATE sbtest SET k = k + 1 WHERE id = ?")
    bindUIntParam(w.bpUpdIndex, w.updIndexId)
    if mysql_stmt_bind_param(w.st[ST_UPD_INDEX]!, w.bpUpdIndex) {
        dieStmt(w.st[ST_UPD_INDEX]!, "mysql_stmt_bind_param(upd_index)")
    }

    /* Non-index update: params (c, id). */
    w.st[ST_UPD_NONINDEX] = prepareStmt(conn, "UPDATE sbtest SET c = ? WHERE id = ?")
    w.bpUpdNonindex.pointee = MYSQL_BIND()
    w.bpUpdNonindex.pointee.buffer_type = MYSQL_TYPE_STRING
    w.bpUpdNonindex.pointee.buffer = UnsafeMutableRawPointer(cValBuf)
    w.bpUpdNonindex.pointee.buffer_length = cValLen.pointee
    w.bpUpdNonindex.pointee.length = cValLen
    bindUIntParam(w.bpUpdNonindex + 1, w.updNonindexId)
    if mysql_stmt_bind_param(w.st[ST_UPD_NONINDEX]!, w.bpUpdNonindex) {
        dieStmt(w.st[ST_UPD_NONINDEX]!, "mysql_stmt_bind_param(upd_nonindex)")
    }

    /* Delete. */
    w.st[ST_DELETE] = prepareStmt(conn, "DELETE FROM sbtest WHERE id = ?")
    bindUIntParam(w.bpDel, w.delId)
    if mysql_stmt_bind_param(w.st[ST_DELETE]!, w.bpDel) {
        dieStmt(w.st[ST_DELETE]!, "mysql_stmt_bind_param(delete)")
    }

    /* Insert: params (id, k, c, pad). */
    w.st[ST_INSERT] = prepareStmt(
        conn, "INSERT INTO sbtest (id, k, c, pad) VALUES (?, ?, ?, ?)")
    bindUIntParam(w.bpIns, w.insId)
    bindUIntParam(w.bpIns + 1, w.insK)
    (w.bpIns + 2).pointee = MYSQL_BIND()
    (w.bpIns + 2).pointee.buffer_type = MYSQL_TYPE_STRING
    (w.bpIns + 2).pointee.buffer = UnsafeMutableRawPointer(cValBuf)
    (w.bpIns + 2).pointee.buffer_length = cValLen.pointee
    (w.bpIns + 2).pointee.length = cValLen
    (w.bpIns + 3).pointee = MYSQL_BIND()
    (w.bpIns + 3).pointee.buffer_type = MYSQL_TYPE_STRING
    (w.bpIns + 3).pointee.buffer = UnsafeMutableRawPointer(padValBuf)
    (w.bpIns + 3).pointee.buffer_length = padValLen.pointee
    (w.bpIns + 3).pointee.length = padValLen
    if mysql_stmt_bind_param(w.st[ST_INSERT]!, w.bpIns) {
        dieStmt(w.st[ST_INSERT]!, "mysql_stmt_bind_param(insert)")
    }
}

/* ---------------- transaction execution ------------------------------------ */

/* Returns true on a retryable concurrency error, false on success; dies otherwise. */
func execDml(_ st: MySQLStmt, _ what: String) -> Bool {
    if mysql_stmt_execute(st) != 0 {
        let e = mysql_stmt_errno(st)
        if errRetryable(e) { return true }
        dieStmt(st, what)
    }
    return false
}

/* Execute a SELECT and fetch all rows to MYSQL_NO_DATA.
   Returns true on a retryable concurrency error, false on success; dies otherwise. */
func execSelect(_ st: MySQLStmt, _ what: String) -> Bool {
    if mysql_stmt_execute(st) != 0 {
        let e = mysql_stmt_errno(st)
        if errRetryable(e) { return true }
        dieStmt(st, what)
    }
    while true {
        let rc = mysql_stmt_fetch(st)
        if rc == 0 || rc == MYSQL_DATA_TRUNCATED {
            continue /* row consumed */
        }
        if rc == MYSQL_NO_DATA {
            return false
        }
        /* rc == 1: error */
        let e = mysql_stmt_errno(st)
        if errRetryable(e) { return true }
        dieStmt(st, what)
    }
}

let rangeWhat = [
    "simple range SELECT", "sum range SELECT",
    "order range SELECT", "distinct range SELECT",
]

/* One attempt at the transaction body (the 18 inner statements).
   Values are drawn immediately before each statement executes, so a retry
   naturally redraws everything (the PRNG stream continues).
   Returns true if the attempt must be retried, false on success; dies otherwise. */
func txnBody(_ w: Worker) -> Bool {
    /* 10 point selects */
    for j in 0..<10 {
        w.pointId[j] = UInt32(1 + prngRand(w, tableRows))
        if execSelect(w.st[ST_POINT0 + j]!, "point SELECT") { return true }
    }

    /* 4 range selects: simple, sum, order, distinct */
    for j in 0..<4 {
        let lo = 1 + prngRand(w, tableRows)
        w.rangeLo[j] = UInt32(lo)
        w.rangeHi[j] = UInt32(lo + rangeSize - 1)
        if execSelect(w.st[ST_RANGE_SIMPLE + j]!, rangeWhat[j]) { return true }
    }

    /* index update */
    w.updIndexId.pointee = UInt32(1 + prngRand(w, tableRows))
    if execDml(w.st[ST_UPD_INDEX]!, "index UPDATE") { return true }

    /* non-index update */
    w.updNonindexId.pointee = UInt32(1 + prngRand(w, tableRows))
    if execDml(w.st[ST_UPD_NONINDEX]!, "non-index UPDATE") { return true }

    /* delete + insert (same id; k drawn with it, before the DELETE) */
    let id = 1 + prngRand(w, tableRows)
    let k = prngRand(w, tableRows)
    w.delId.pointee = UInt32(id)
    if execDml(w.st[ST_DELETE]!, "DELETE") { return true }
    w.insId.pointee = UInt32(id)
    w.insK.pointee = UInt32(k)
    if execDml(w.st[ST_INSERT]!, "INSERT") { return true }

    return false
}

/* Run one full transaction until it commits. Retryable errors (1213/1205/1062)
   anywhere in it — including BEGIN/COMMIT — roll back, bump retries, redraw
   and rerun. */
func runTxn(_ w: Worker) {
    while true {
        if mysql_query(w.conn, "BEGIN") != 0 {
            let e = mysql_errno(w.conn)
            if !errRetryable(e) { dieMysql(w.conn, "BEGIN") }
            _ = mysql_query(w.conn, "ROLLBACK") /* ignore result */
            w.retries += 1
            continue
        }
        if txnBody(w) {
            _ = mysql_query(w.conn, "ROLLBACK") /* ignore result */
            w.retries += 1
            continue
        }
        if mysql_query(w.conn, "COMMIT") != 0 {
            let e = mysql_errno(w.conn)
            if !errRetryable(e) { dieMysql(w.conn, "COMMIT") }
            _ = mysql_query(w.conn, "ROLLBACK") /* ignore result */
            w.retries += 1
            continue
        }
        return
    }
}

/* ---------------- worker thread -------------------------------------------- */

func workerMain(_ w: Worker) {
    workerConnect(w)
    workerPrepare(w)

    /* Warmup: executed, same PRNG stream, not timed. */
    for _ in 0..<warmupTxns {
        runTxn(w)
    }
    /* Reported retries cover only the timed window (all impls agree). */
    w.retries = 0

    /* Wait for all workers (and main) at the start barrier. */
    _ = pthread_barrier_wait(startBarrier)

    /* Timed loop: per-transaction latency in integer microseconds, from just
       before the first BEGIN attempt to after the successful COMMIT. */
    for i in 0..<txnsPerThread {
        let t0 = nowNs()
        runTxn(w)
        w.lat[i] = (nowNs() - t0) / 1000
    }

    for j in 0..<ST_COUNT {
        _ = mysql_stmt_close(w.st[j]!)
    }
    mysql_close(w.conn)
    mysql_thread_end()
    doneSem.signal()
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
    tableRows = envInt("BENCH_TABLE_ROWS")
    nThreads = envInt("BENCH_THREADS")
    warmupTxns = envInt("BENCH_WARMUP_TXNS")
    txnsPerThread = envInt("BENCH_TXNS_PER_THREAD")
    rangeSize = envInt("BENCH_RANGE_SIZE")
    baseSeed = envInt("BENCH_SEED")

    if nThreads <= 0 || txnsPerThread <= 0 || tableRows <= 0 ||
        rangeSize <= 0 || warmupTxns < 0 {
        fail("invalid workload configuration")
    }

    /* Must be called before any threads touch the client library.
       (mysql_library_init is a macro aliasing mysql_server_init.) */
    if mysql_server_init(0, nil, nil) != 0 {
        fail("mysql_library_init failed")
    }

    /* Shared constant string parameters (C_VAL 120 chars, PAD_VAL 60 chars). */
    let cVal = String(repeating: "abcdefghij", count: 12)
    let padVal = String(repeating: "0123456789", count: 6)
    cValBuf = makeCBuf(cVal)
    cValLen = UnsafeMutablePointer<UInt>.allocate(capacity: 1)
    cValLen.initialize(to: UInt(cVal.utf8.count))   /* 120 */
    padValBuf = makeCBuf(padVal)
    padValLen = UnsafeMutablePointer<UInt>.allocate(capacity: 1)
    padValLen.initialize(to: UInt(padVal.utf8.count)) /* 60 */

    let workers = (0..<nThreads).map {
        Worker(index: $0, seed: baseSeed + $0, txns: txnsPerThread)
    }

    startBarrier = UnsafeMutablePointer<pthread_barrier_t>.allocate(capacity: 1)
    startBarrier.initialize(to: pthread_barrier_t())
    if pthread_barrier_init(startBarrier, nil, UInt32(nThreads + 1)) != 0 {
        fail("pthread_barrier_init failed")
    }

    for w in workers {
        let t = Thread { workerMain(w) }
        t.start()
    }

    /* Workers connect, prepare and warm up, then block on the barrier.
       When the last one arrives, everybody is released: sample t_start. */
    _ = pthread_barrier_wait(startBarrier)
    let tStart = nowNs()

    for _ in 0..<nThreads {
        doneSem.wait()
    }
    let tEnd = nowNs()

    _ = pthread_barrier_destroy(startBarrier)

    let driverVersion = String(cString: mysql_get_client_info())

    /* Merge latency arrays, sort, percentiles. */
    let txns = nThreads * txnsPerThread
    var lat = [Int64](repeating: 0, count: txns)
    var retries: Int64 = 0
    var idx = 0
    for w in workers {
        for i in 0..<txnsPerThread {
            lat[idx] = w.lat[i]
            idx += 1
        }
        retries += w.retries
    }
    lat.sort()
    let p50 = percentile(lat, 50)
    let p95 = percentile(lat, 95)
    let p99 = percentile(lat, 99)
    let maxUs = lat[txns - 1]

    let elapsedS = Double(tEnd - tStart) / 1e9
    let tps = Double(txns) / elapsedS
    let qps = tps * 20.0 /* 18 statements + BEGIN + COMMIT */

    var hostBuf = [CChar](repeating: 0, count: 256)
    var clientHost = "unknown"
    if gethostname(&hostBuf, 255) == 0 {
        clientHost = hostBuf.withUnsafeBufferPointer { String(cString: $0.baseAddress!) }
    }

    /* One JSON line, exact v2 field order. */
    var json = "{\"lang\":\"swift\",\"driver\":\"libmysqlclient\""
    json += ",\"driver_version\":\"\(driverVersion)\""
    json += ",\"runtime_version\":\"\(runtimeVersion)\""
    json += ",\"prepared\":true"
    json += ",\"client_host\":\"\(clientHost)\""
    json += ",\"db_host\":\"\(cfgHost)\""
    json += ",\"threads\":\(nThreads)"
    json += ",\"txns\":\(txns)"
    json += ",\"retries\":\(retries)"
    json += ",\"errors\":0"
    json += ",\"elapsed_s\":\(String(format: "%.3f", elapsedS))"
    json += ",\"tps\":\(String(format: "%.1f", tps))"
    json += ",\"qps\":\(String(format: "%.1f", qps))"
    json += ",\"p50_us\":\(p50)"
    json += ",\"p95_us\":\(p95)"
    json += ",\"p99_us\":\(p99)"
    json += ",\"max_us\":\(maxUs)}"
    print(json)

    for w in workers {
        w.cleanup()
    }
    startBarrier.deallocate()
    cValBuf.deallocate(); cValLen.deallocate()
    padValBuf.deallocate(); padValLen.deallocate()
    mysql_server_end()
}

main()
