import java.net.InetAddress;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.Arrays;
import java.util.Locale;
import java.util.concurrent.CyclicBarrier;

public class Bench {

    // ---- configuration, set in main() before any worker starts ----
    private static String url;
    private static String user;
    private static String password;
    private static int tableRows;
    private static int threads;
    private static int warmupTxns;
    private static int txnsPerThread;
    private static int durationS;
    private static int rangeSize;
    private static int seed;
    private static String cVal;
    private static String padVal;
    private static CyclicBarrier startBarrier;

    private static volatile String driverVersion = "unknown";

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

    // p(x) = arr[ceil(x/100 * n) - 1], 0-based, array sorted ascending
    private static long percentile(long[] sorted, int x) {
        long n = sorted.length;
        int idx = (int) (((long) x * n + 99L) / 100L) - 1;
        if (idx < 0) idx = 0;
        return sorted[idx];
    }

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

    private static final class Worker extends Thread {

        private final int idx;
        long[] latencies;            // events mode: exact; duration mode: grown by doubling
        int count;                   // number of timed txns recorded in latencies
        long retries;                // retryable-error count (timed phase)

        // 31-bit LCG per SPEC.md, one independent stream per worker.
        private long lcgState;

        private Statement plain;     // BEGIN / COMMIT / ROLLBACK as plain queries
        private final PreparedStatement[] pointSel = new PreparedStatement[10];
        private PreparedStatement rangeSel;
        private PreparedStatement sumSel;
        private PreparedStatement orderSel;
        private PreparedStatement distinctSel;
        private PreparedStatement idxUpd;
        private PreparedStatement nonIdxUpd;
        private PreparedStatement del;
        private PreparedStatement ins;

        Worker(int idx) {
            super("bench-worker-" + idx);
            this.idx = idx;
            this.latencies = new long[durationS > 0 ? 4096 : txnsPerThread];
            this.lcgState = (long) seed + idx;
        }

        private long rand(long n) { // uniform 0..n-1
            lcgState = (lcgState * 1103515245L + 12345L) % 2147483648L; // 2^31
            return lcgState % n;
        }

        @Override
        public void run() {
            Connection conn = null;
            try {
                conn = DriverManager.getConnection(url, user, password);
                if (idx == 0) {
                    driverVersion = conn.getMetaData().getDriverVersion();
                }
                plain = conn.createStatement();
                for (int i = 0; i < 10; i++) {
                    pointSel[i] = conn.prepareStatement("SELECT c FROM sbtest WHERE id = ?");
                }
                rangeSel = conn.prepareStatement("SELECT c FROM sbtest WHERE id BETWEEN ? AND ?");
                sumSel = conn.prepareStatement("SELECT SUM(k) FROM sbtest WHERE id BETWEEN ? AND ?");
                orderSel = conn.prepareStatement("SELECT c FROM sbtest WHERE id BETWEEN ? AND ? ORDER BY c");
                distinctSel = conn.prepareStatement("SELECT DISTINCT c FROM sbtest WHERE id BETWEEN ? AND ? ORDER BY c");
                idxUpd = conn.prepareStatement("UPDATE sbtest SET k = k + 1 WHERE id = ?");
                nonIdxUpd = conn.prepareStatement("UPDATE sbtest SET c = ? WHERE id = ?");
                del = conn.prepareStatement("DELETE FROM sbtest WHERE id = ?");
                ins = conn.prepareStatement("INSERT INTO sbtest (id, k, c, pad) VALUES (?, ?, ?, ?)");

                for (int i = 0; i < warmupTxns; i++) {
                    runTxnWithRetries(false);
                }

                startBarrier.await(); // released together with main's t_start sample

                if (durationS > 0) {
                    long start = System.nanoTime();
                    long deadline = start + durationS * 1_000_000_000L;
                    // deadline checked before each new txn; an in-flight txn
                    // (retries included) runs to successful commit
                    while (System.nanoTime() < deadline) {
                        long t0 = System.nanoTime();
                        runTxnWithRetries(true);
                        long t1 = System.nanoTime();
                        record((t1 - t0) / 1000L); // integer microseconds, retries included
                    }
                } else {
                    for (int i = 0; i < txnsPerThread; i++) {
                        long t0 = System.nanoTime();
                        runTxnWithRetries(true);
                        long t1 = System.nanoTime();
                        record((t1 - t0) / 1000L); // integer microseconds, retries included
                    }
                }

                conn.close();
            } catch (SQLException e) {
                System.err.println("worker " + idx + " error " + e.getErrorCode() + ": " + e);
                System.exit(1);
            } catch (Exception e) { // InterruptedException, BrokenBarrierException, ...
                System.err.println("worker " + idx + " error: " + e);
                System.exit(1);
            }
        }

        private void record(long us) {
            if (count == latencies.length) {
                latencies = Arrays.copyOf(latencies, latencies.length * 2);
            }
            latencies[count++] = us;
        }

        // Runs one transaction, retrying on deadlock (1213), lock wait timeout (1205)
        // and duplicate key (1062) with all values redrawn (the PRNG stream continues).
        private void runTxnWithRetries(boolean timed) throws SQLException {
            for (;;) {
                try {
                    txnOnce();
                    return;
                } catch (SQLException e) {
                    int code = e.getErrorCode();
                    if (code == 1213 || code == 1205 || code == 1062) {
                        try {
                            plain.execute("ROLLBACK");
                        } catch (SQLException ignored) {
                            // rollback errors are ignored per spec
                        }
                        if (timed) retries++;
                        // loop: re-run the whole transaction with fresh draws
                    } else {
                        throw e;
                    }
                }
            }
        }

        private void fetchAll(PreparedStatement ps) throws SQLException {
            ResultSet rs = ps.executeQuery();
            try {
                while (rs.next()) {
                    rs.getString(1); // fetch every row; getString is DECIMAL-safe for SUM(k)
                }
            } finally {
                rs.close();
            }
        }

        // One sysbench oltp_read_write transaction; values drawn in execution order.
        private void txnOnce() throws SQLException {
            plain.execute("BEGIN");

            for (int i = 0; i < 10; i++) {
                long id = 1 + rand(tableRows);
                pointSel[i].setLong(1, id);
                fetchAll(pointSel[i]);
            }

            long lo = 1 + rand(tableRows);
            rangeSel.setLong(1, lo);
            rangeSel.setLong(2, lo + rangeSize - 1);
            fetchAll(rangeSel);

            lo = 1 + rand(tableRows);
            sumSel.setLong(1, lo);
            sumSel.setLong(2, lo + rangeSize - 1);
            fetchAll(sumSel);

            lo = 1 + rand(tableRows);
            orderSel.setLong(1, lo);
            orderSel.setLong(2, lo + rangeSize - 1);
            fetchAll(orderSel);

            lo = 1 + rand(tableRows);
            distinctSel.setLong(1, lo);
            distinctSel.setLong(2, lo + rangeSize - 1);
            fetchAll(distinctSel);

            long id = 1 + rand(tableRows);
            idxUpd.setLong(1, id);
            idxUpd.executeUpdate();

            id = 1 + rand(tableRows);
            nonIdxUpd.setString(1, cVal);
            nonIdxUpd.setLong(2, id);
            nonIdxUpd.executeUpdate();

            id = 1 + rand(tableRows);
            long k = rand(tableRows);
            del.setLong(1, id);
            del.executeUpdate();
            ins.setLong(1, id);
            ins.setLong(2, k);
            ins.setString(3, cVal);
            ins.setString(4, padVal);
            ins.executeUpdate();

            plain.execute("COMMIT");
        }
    }

    public static void main(String[] args) {
        String host = envStr("MYSQL_HOST");
        String port = envStr("MYSQL_PORT");
        user = envStr("MYSQL_USER");
        password = envStr("MYSQL_PASSWORD");
        String db = envStr("MYSQL_DB");

        tableRows = envInt("BENCH_TABLE_ROWS");
        threads = envInt("BENCH_THREADS");
        warmupTxns = envInt("BENCH_WARMUP_TXNS");
        txnsPerThread = envInt("BENCH_TXNS_PER_THREAD");
        durationS = envIntOpt("BENCH_DURATION_S", 0);
        rangeSize = envInt("BENCH_RANGE_SIZE");
        seed = envInt("BENCH_SEED");

        cVal = repeat("abcdefghij", 12);   // 120 chars
        padVal = repeat("0123456789", 6);  // 60 chars

        url = "jdbc:mysql://" + host + ":" + port + "/" + db
                + "?useServerPrepStmts=true&cachePrepStmts=true&prepStmtCacheSize=64"
                + "&useSSL=false&allowPublicKeyRetrieval=true";

        startBarrier = new CyclicBarrier(threads + 1);

        Worker[] workers = new Worker[threads];
        for (int i = 0; i < threads; i++) {
            workers[i] = new Worker(i);
            workers[i].start();
        }

        long tStart;
        long tEnd;
        try {
            startBarrier.await(); // all workers connected, prepared, warmed up
            tStart = System.nanoTime();
            for (int i = 0; i < threads; i++) {
                workers[i].join();
            }
            tEnd = System.nanoTime();
        } catch (Exception e) { // InterruptedException, BrokenBarrierException
            System.err.println("error: " + e);
            System.exit(1);
            return;
        }

        long txns = 0;
        for (int i = 0; i < threads; i++) {
            txns += workers[i].count;
        }
        long[] all = new long[(int) txns];
        int pos = 0;
        long totalRetries = 0;
        for (int i = 0; i < threads; i++) {
            System.arraycopy(workers[i].latencies, 0, all, pos, workers[i].count);
            pos += workers[i].count;
            totalRetries += workers[i].retries;
        }
        Arrays.sort(all);

        double elapsedS = (tEnd - tStart) / 1_000_000_000.0;
        double tps = txns / elapsedS;
        double qps = tps * 20.0; // 18 statements + BEGIN + COMMIT, sysbench counting

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

        StringBuilder sb = new StringBuilder(512);
        sb.append("{\"lang\":\"java\"");
        sb.append(",\"driver\":\"Connector/J\"");
        sb.append(",\"driver_version\":");
        jsonStr(sb, driverVersion);
        sb.append(",\"runtime_version\":");
        jsonStr(sb, System.getProperty("java.version"));
        sb.append(",\"prepared\":true");
        sb.append(",\"client_host\":");
        jsonStr(sb, clientHost);
        sb.append(",\"db_host\":");
        jsonStr(sb, host);
        sb.append(",\"threads\":").append(threads);
        sb.append(",\"txns\":").append(txns);
        sb.append(",\"retries\":").append(totalRetries);
        sb.append(",\"errors\":0");
        sb.append(",\"elapsed_s\":").append(String.format(Locale.ROOT, "%.3f", elapsedS));
        sb.append(",\"tps\":").append(String.format(Locale.ROOT, "%.1f", tps));
        sb.append(",\"qps\":").append(String.format(Locale.ROOT, "%.1f", qps));
        sb.append(",\"p50_us\":").append(p50);
        sb.append(",\"p95_us\":").append(p95);
        sb.append(",\"p99_us\":").append(p99);
        sb.append(",\"max_us\":").append(maxUs);
        sb.append('}');

        System.out.println(sb.toString());
    }
}
