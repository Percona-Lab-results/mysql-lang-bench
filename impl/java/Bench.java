/*
 * mysql-lang-bench — Java implementation (v3, four micro-workloads).
 * JDBC via MySQL Connector/J, server-side prepared statements. See SPEC.md:
 * one workload selected by BENCH_WORKLOAD (1..4), BENCH_THREADS Java Thread
 * workers each with its own Connection and PRNG stream, CyclicBarrier start,
 * System.nanoTime timing. One JSON line on stdout.
 *
 * Workloads:
 *   1 int-insert   prepared multi-row INSERT of BENCH_INT_BATCH value tuples,
 *                  executed BENCH_INT_ROWS/BENCH_INT_BATCH times.
 *   2 int-read     SELECT val FROM bench_int LIMIT BENCH_INT_ROWS, sum in app.
 *   3 blob-insert  bulk-fill one BENCH_BLOB_SIZE buffer per worker once, stamp
 *                  the blob id into its first 8 bytes, MD5 in app, then INSERT
 *                  bench_blob(id, data) with id = i*BLOB_COUNT + k.
 *   4 blob-read    SELECT data WHERE id = ?, MD5 the content in app.
 *
 * Decisions (documented so the other 12 impls can match):
 *   - Blob content: java.util.Random.nextBytes fills one reused buffer once per
 *     worker (fast, not a per-byte loop, per SPEC); each blob's first 8 bytes
 *     are overwritten with the id so every blob (and its MD5) differs.
 *   - Per-op latency wraps ONLY the SQL statement (executeUpdate /
 *     executeQuery+fetch); random generation and MD5 are outside the latency
 *     sample but inside the overall timed window (they count toward elapsed_s
 *     and therefore throughput). WL2's single sample wraps the whole scan.
 *   - Autocommit left ON (Connector/J default): SPEC permits autocommit for
 *     the write workloads ("or autocommit — a worker may batch its whole load
 *     in one txn").
 */

import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.Arrays;
import java.util.Locale;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicLongArray;

public class Bench {

    // ---- configuration, set in main() before any worker starts ----
    private static String url;
    private static String user;
    private static String password;
    private static String host;

    private static int workload;
    private static int threads;
    private static int seed;
    private static int intRows;
    private static int intBatch;
    private static int blobCount;
    private static int blobSize;

    private static String workloadName;
    private static String unit;
    private static boolean blobWorkload;

    private static CyclicBarrier startBarrier;
    private static volatile String driverVersion = "unknown";

    // ---- intermediate-progress checkpointing ----
    private static boolean progressEnabled;
    private static String progressFile;
    private static int progressSec = 5;
    private static AtomicLongArray progress;      // per-worker completed-ops slot
    private static volatile long tStartNs;        // sampled by main at barrier release
    private static volatile boolean finished;     // signals the monitor to stop

    // Black-box sink: prevents the JIT from eliding sums / MD5 digests.
    private static final AtomicLong SINK = new AtomicLong();

    private static final char[] HEX = "0123456789abcdef".toCharArray();

    // ---------------- env parsing ----------------

    private static int envInt(String name) {
        String v = System.getenv(name);
        if (v == null || v.isEmpty()) {
            System.err.println("missing required environment variable: " + name);
            System.exit(1);
        }
        return Integer.parseInt(v.trim());
    }

    private static int envIntOpt(String name, int def) {
        String v = System.getenv(name);
        if (v == null || v.isEmpty()) return def;
        return Integer.parseInt(v.trim());
    }

    private static String envStr(String name) {
        String v = System.getenv(name);
        if (v == null) {
            System.err.println("missing required environment variable: " + name);
            System.exit(1);
        }
        return v;
    }

    private static String repeat(String s, int times) {
        StringBuilder sb = new StringBuilder(s.length() * times);
        for (int i = 0; i < times; i++) sb.append(s);
        return sb.toString();
    }

    // ---------------- stats ----------------

    // p(x) = arr[ceil(x/100 * n) - 1], 0-based, array sorted ascending
    private static long percentile(long[] sorted, int x) {
        long n = sorted.length;
        int idx = (int) (((long) x * n + 99L) / 100L) - 1;
        if (idx < 0) idx = 0;
        return sorted[idx];
    }

    private static String fmt1(double d) { return String.format(Locale.ROOT, "%.1f", d); }
    private static String fmt3(double d) { return String.format(Locale.ROOT, "%.3f", d); }

    private static void jsonStr(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"' || c == '\\') {
                sb.append('\\').append(c);
            } else if (c < 0x20) {
                sb.append(String.format(Locale.ROOT, "\\u%04x", (int) c));
            } else {
                sb.append(c);
            }
        }
        sb.append('"');
    }

    private static String toHex(byte[] d, int len) {
        char[] c = new char[len * 2];
        for (int i = 0; i < len; i++) {
            int v = d[i] & 0xff;
            c[i * 2] = HEX[v >>> 4];
            c[i * 2 + 1] = HEX[v & 0xf];
        }
        return new String(c);
    }

    // ---------------- intermediate progress ----------------

    private static void writeProgress() throws Exception {
        long ops = 0;
        for (int i = 0; i < threads; i++) ops += progress.get(i);
        double elapsed = (System.nanoTime() - tStartNs) / 1_000_000_000.0;
        double tput = elapsed > 0 ? ops / elapsed : 0.0;

        StringBuilder sb = new StringBuilder(256);
        sb.append("{\"workload\":").append(workload);
        sb.append(",\"workload_name\":"); jsonStr(sb, workloadName);
        sb.append(",\"unit\":"); jsonStr(sb, unit);
        sb.append(",\"threads\":").append(threads);
        sb.append(",\"ops\":").append(ops);
        sb.append(",\"elapsed_s\":").append(fmt3(elapsed));
        sb.append(",\"throughput\":").append(fmt1(tput));
        sb.append(",\"partial\":true}");

        Path target = Paths.get(progressFile);
        Path tmp = Paths.get(progressFile + ".tmp");
        Files.write(tmp, sb.toString().getBytes(StandardCharsets.UTF_8));
        try {
            Files.move(tmp, target, StandardCopyOption.ATOMIC_MOVE);
        } catch (Exception e) {
            // Fall back if the filesystem can't do an atomic rename.
            Files.move(tmp, target, StandardCopyOption.REPLACE_EXISTING);
        }
    }

    // ---------------- worker ----------------

    private static final class Worker extends Thread {

        private final int idx;
        long[] latencies;   // exactly sized per workload
        int count;          // latency samples recorded
        long opsDone;       // completed operations (rows / blobs) for this worker
        long sink;          // local black-box accumulator

        // 31-bit LCG per SPEC.md, one independent stream per worker.
        private long lcgState;

        Worker(int idx) {
            super("bench-worker-" + idx);
            this.idx = idx;
            this.lcgState = (long) seed + idx;
            switch (workload) {
                case 1:  latencies = new long[intRows / intBatch]; break;
                case 2:  latencies = new long[1];                  break;
                default: latencies = new long[blobCount];          break; // 3, 4
            }
        }

        // next(): state = (state*1103515245 + 12345) mod 2^31; return state.
        // state stays in [0, 2^31), so the product fits a signed 64-bit long.
        private long next() {
            lcgState = (lcgState * 1103515245L + 12345L) & 0x7fffffffL;
            return lcgState;
        }

        private void record(long us) {
            latencies[count++] = us;
        }

        @Override
        public void run() {
            Connection conn = null;
            try {
                conn = DriverManager.getConnection(url, user, password);
                if (idx == 0) {
                    driverVersion = conn.getMetaData().getDriverVersion();
                }
                switch (workload) {
                    case 1: workload1(conn); break;
                    case 2: workload2(conn); break;
                    case 3: workload3(conn); break;
                    case 4: workload4(conn); break;
                    default:
                        System.err.println("invalid BENCH_WORKLOAD: " + workload);
                        System.exit(1);
                }
                SINK.addAndGet(sink);
                conn.close();
            } catch (SQLException e) {
                System.err.println("worker " + idx + " SQL error "
                        + e.getErrorCode() + ": " + e);
                System.exit(1);
            } catch (Exception e) { // InterruptedException, BrokenBarrierException, ...
                System.err.println("worker " + idx + " error: " + e);
                System.exit(1);
            }
        }

        // WL1 — integer insert: prepared multi-row INSERT of intBatch tuples.
        private void workload1(Connection conn) throws Exception {
            StringBuilder q = new StringBuilder("INSERT INTO bench_int (val) VALUES ");
            for (int i = 0; i < intBatch; i++) {
                if (i > 0) q.append(',');
                q.append("(?)");
            }
            PreparedStatement ins = conn.prepareStatement(q.toString());
            int iterations = intRows / intBatch;

            startBarrier.await();

            long rows = 0;
            for (int it = 0; it < iterations; it++) {
                for (int b = 0; b < intBatch; b++) {
                    ins.setInt(b + 1, (int) next());
                }
                long t0 = System.nanoTime();
                ins.executeUpdate();
                long t1 = System.nanoTime();
                record((t1 - t0) / 1000L);
                rows += intBatch;
                if (progressEnabled) progress.set(idx, rows);
            }
            ins.close();
            opsDone = rows;
        }

        // WL2 — integer fetch + sum.
        private void workload2(Connection conn) throws Exception {
            PreparedStatement sel = conn.prepareStatement(
                    "SELECT val FROM bench_int LIMIT " + intRows);

            startBarrier.await();

            long rows = 0;
            long s = 0;
            long t0 = System.nanoTime();
            ResultSet rs = sel.executeQuery();
            while (rs.next()) {
                s += rs.getInt(1);
                rows++;
                if (progressEnabled && (rows & 0xFFFF) == 0) progress.set(idx, rows);
            }
            rs.close();
            long t1 = System.nanoTime();
            record((t1 - t0) / 1000L);
            sel.close();

            if (progressEnabled) progress.set(idx, rows);
            sink = s;
            opsDone = rows;
        }

        // WL3 — blob insert + MD5.
        private void workload3(Connection conn) throws Exception {
            MessageDigest md = MessageDigest.getInstance("MD5");
            PreparedStatement ins = conn.prepareStatement(
                    "INSERT INTO bench_blob (id, data) VALUES (?, ?)");

            // SPEC: generate the buffer ONCE per worker with a fast native
            // random fill (never a per-byte interpreted loop), then before each
            // blob overwrite the first 8 bytes with the id so each blob and its
            // MD5 differ. Seeded for determinism; content need not match other
            // languages. This fill is outside the timed window (before barrier).
            byte[] buf = new byte[blobSize];
            new java.util.Random((long) seed + idx).nextBytes(buf);

            startBarrier.await();

            long s = 0;
            for (int k = 0; k < blobCount; k++) {
                long id = (long) idx * blobCount + k;
                // stamp id into the first 8 bytes (little-endian)
                buf[0] = (byte) id;
                buf[1] = (byte) (id >>> 8);
                buf[2] = (byte) (id >>> 16);
                buf[3] = (byte) (id >>> 24);
                buf[4] = (byte) (id >>> 32);
                buf[5] = (byte) (id >>> 40);
                buf[6] = (byte) (id >>> 48);
                buf[7] = (byte) (id >>> 56);
                md.reset();
                md.update(buf);
                byte[] dig = md.digest();
                s += toHex(dig, dig.length).hashCode();

                ins.setLong(1, id);
                ins.setBytes(2, buf);
                long t0 = System.nanoTime();
                ins.executeUpdate();
                long t1 = System.nanoTime();
                record((t1 - t0) / 1000L);
                if (progressEnabled) progress.set(idx, k + 1);
            }
            ins.close();
            sink = s;
            opsDone = blobCount;
        }

        // WL4 — blob read + MD5.
        private void workload4(Connection conn) throws Exception {
            MessageDigest md = MessageDigest.getInstance("MD5");
            PreparedStatement sel = conn.prepareStatement(
                    "SELECT data FROM bench_blob WHERE id = ?");

            startBarrier.await();

            long s = 0;
            for (int k = 0; k < blobCount; k++) {
                long id = (long) idx * blobCount + k;
                sel.setLong(1, id);
                long t0 = System.nanoTime();
                ResultSet rs = sel.executeQuery();
                if (!rs.next()) {
                    rs.close();
                    throw new SQLException("missing blob id " + id);
                }
                byte[] data = rs.getBytes(1);
                rs.close();
                long t1 = System.nanoTime();
                record((t1 - t0) / 1000L);

                md.reset();
                md.update(data);
                byte[] dig = md.digest();
                s += toHex(dig, dig.length).hashCode();
                if (progressEnabled) progress.set(idx, k + 1);
            }
            sel.close();
            sink = s;
            opsDone = blobCount;
        }
    }

    // ---------------- main ----------------

    public static void main(String[] args) {
        host = envStr("MYSQL_HOST");
        String port = envStr("MYSQL_PORT");
        user = envStr("MYSQL_USER");
        password = envStr("MYSQL_PASSWORD");
        String db = envStr("MYSQL_DB");

        workload = envInt("BENCH_WORKLOAD");
        threads = envInt("BENCH_THREADS");
        seed = envInt("BENCH_SEED");
        intRows = envIntOpt("BENCH_INT_ROWS", 1000000);
        intBatch = envIntOpt("BENCH_INT_BATCH", 10);
        blobCount = envIntOpt("BENCH_BLOB_COUNT", 100);
        blobSize = envIntOpt("BENCH_BLOB_SIZE", 1048576);

        if (threads <= 0) { System.err.println("BENCH_THREADS must be > 0"); System.exit(1); }
        if (intBatch <= 0 || intRows <= 0) { System.err.println("bad BENCH_INT_ROWS/BATCH"); System.exit(1); }

        switch (workload) {
            case 1: workloadName = "int-insert";  unit = "rows/s";  blobWorkload = false; break;
            case 2: workloadName = "int-read";    unit = "rows/s";  blobWorkload = false; break;
            case 3: workloadName = "blob-insert"; unit = "blobs/s"; blobWorkload = true;  break;
            case 4: workloadName = "blob-read";   unit = "blobs/s"; blobWorkload = true;  break;
            default:
                System.err.println("BENCH_WORKLOAD must be 1..4, got " + workload);
                System.exit(1);
                return;
        }

        progressFile = System.getenv("BENCH_PROGRESS_FILE");
        progressEnabled = progressFile != null && !progressFile.isEmpty();
        progressSec = envIntOpt("BENCH_PROGRESS_SEC", 5);
        if (progressSec <= 0) progressSec = 5;
        if (progressEnabled) progress = new AtomicLongArray(threads);

        // JDBC URL options as v2: server-side prepared statements, plaintext.
        url = "jdbc:mysql://" + host + ":" + port + "/" + db
                + "?useServerPrepStmts=true&cachePrepStmts=true"
                + "&useSSL=false&allowPublicKeyRetrieval=true";

        startBarrier = new CyclicBarrier(threads + 1);

        Worker[] workers = new Worker[threads];
        for (int i = 0; i < threads; i++) {
            workers[i] = new Worker(i);
            workers[i].start();
        }

        Thread monitor = null;
        long tStart;
        long tEnd;
        try {
            startBarrier.await(); // all workers connected and prepared
            tStart = System.nanoTime();
            tStartNs = tStart;

            if (progressEnabled) {
                monitor = new Thread(new Runnable() {
                    public void run() {
                        try {
                            while (!finished) {
                                Thread.sleep(progressSec * 1000L);
                                if (finished) break;
                                try {
                                    writeProgress();
                                } catch (Exception e) {
                                    // Progress is auxiliary: a transient write
                                    // failure must not abort a healthy run.
                                    System.err.println("progress write failed: " + e);
                                }
                            }
                        } catch (InterruptedException ignored) {
                            // asked to stop
                        }
                    }
                }, "bench-progress");
                monitor.setDaemon(true);
                monitor.start();
            }

            for (int i = 0; i < threads; i++) {
                workers[i].join();
            }
            tEnd = System.nanoTime();
        } catch (Exception e) { // InterruptedException, BrokenBarrierException
            System.err.println("error: " + e);
            System.exit(1);
            return;
        }

        finished = true;
        if (monitor != null) {
            monitor.interrupt();
            try { monitor.join(); } catch (InterruptedException ignored) { }
        }

        // Merge latency samples, sort, percentiles.
        long total = 0;
        long ops = 0;
        for (int i = 0; i < threads; i++) {
            total += workers[i].count;
            ops += workers[i].opsDone;
        }
        long[] all = new long[(int) total];
        int pos = 0;
        for (int i = 0; i < threads; i++) {
            System.arraycopy(workers[i].latencies, 0, all, pos, workers[i].count);
            pos += workers[i].count;
        }
        Arrays.sort(all);

        double elapsedS = (tEnd - tStart) / 1_000_000_000.0;
        double throughput = ops / elapsedS;
        double mbPerS = (double) ops * blobSize / 1_000_000.0 / elapsedS;

        long p50 = percentile(all, 50);
        long p95 = percentile(all, 95);
        long p99 = percentile(all, 99);
        long maxUs = all[all.length - 1];

        String clientHost;
        try {
            clientHost = InetAddress.getLocalHost().getHostName();
        } catch (Exception e) {
            clientHost = System.getenv("HOSTNAME");
            if (clientHost == null || clientHost.isEmpty()) clientHost = "unknown";
        }

        StringBuilder sb = new StringBuilder(640);
        sb.append("{\"lang\":\"java\"");
        sb.append(",\"workload\":").append(workload);
        sb.append(",\"workload_name\":"); jsonStr(sb, workloadName);
        sb.append(",\"unit\":"); jsonStr(sb, unit);
        sb.append(",\"driver\":\"Connector/J\"");
        sb.append(",\"driver_version\":"); jsonStr(sb, driverVersion);
        sb.append(",\"runtime_version\":"); jsonStr(sb, System.getProperty("java.version"));
        sb.append(",\"prepared\":true");
        sb.append(",\"client_host\":"); jsonStr(sb, clientHost);
        sb.append(",\"db_host\":"); jsonStr(sb, host);
        sb.append(",\"threads\":").append(threads);
        sb.append(",\"ops\":").append(ops);
        sb.append(",\"errors\":0");
        sb.append(",\"elapsed_s\":").append(fmt3(elapsedS));
        sb.append(",\"throughput\":").append(fmt1(throughput));
        sb.append(",\"mb_per_s\":").append(blobWorkload ? fmt1(mbPerS) : "0");
        sb.append(",\"p50_us\":").append(p50);
        sb.append(",\"p95_us\":").append(p95);
        sb.append(",\"p99_us\":").append(p99);
        sb.append(",\"max_us\":").append(maxUs);
        sb.append(",\"client_cpu_s\":0");
        sb.append(",\"server_cpu_s\":0");
        sb.append('}');

        System.out.println(sb.toString());

        // Final JSON is on stdout; the checkpoint file is now superseded.
        if (progressEnabled) {
            try {
                Files.deleteIfExists(Paths.get(progressFile));
                Files.deleteIfExists(Paths.get(progressFile + ".tmp"));
            } catch (Exception ignored) {
                // harness also removes it; a leftover file is harmless
            }
        }
    }
}
