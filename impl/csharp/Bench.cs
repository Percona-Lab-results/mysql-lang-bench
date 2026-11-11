// C# implementation of the v2 multithreaded oltp_read_write benchmark (see SPEC.md).
// Driver: MySqlConnector. One dedicated OS thread + one dedicated connection per worker,
// server-side prepared statements, explicit plain BEGIN/COMMIT/ROLLBACK.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Net;
using System.Text;
using System.Threading;
using MySqlConnector;

internal static class Bench
{
    // Keep in sync with the MySqlConnector PackageReference version in bench.csproj.
    private const string DriverVersion = "2.4.0";

    // ---- configuration, set in Main() before any worker starts ----
    private static string _connString = "";
    private static string _dbHost = "";
    private static int _tableRows;
    private static int _threads;
    private static int _warmupTxns;
    private static int _txnsPerThread;
    private static int _durationS; // 0 = events mode, > 0 = fixed-duration mode
    private static int _rangeSize;
    private static int _seed;
    private static string _cVal = "";
    private static string _padVal = "";
    private static Barrier _startBarrier = null!;

    private static string EnvStr(string name)
    {
        string? v = Environment.GetEnvironmentVariable(name);
        if (v is null)
        {
            Console.Error.WriteLine("missing required environment variable: " + name);
            Environment.Exit(1);
        }
        return v!;
    }

    private static int EnvInt(string name)
    {
        string v = EnvStr(name).Trim();
        if (v.Length == 0)
        {
            Console.Error.WriteLine("missing required environment variable: " + name);
            Environment.Exit(1);
        }
        return int.Parse(v, NumberStyles.Integer, CultureInfo.InvariantCulture);
    }

    private static int EnvIntOpt(string name, int def)
    {
        string? v = Environment.GetEnvironmentVariable(name);
        if (v is null) return def;
        v = v.Trim();
        if (v.Length == 0) return def;
        return int.Parse(v, NumberStyles.Integer, CultureInfo.InvariantCulture);
    }

    private static string Repeat(string s, int times)
    {
        var sb = new StringBuilder(s.Length * times);
        for (int i = 0; i < times; i++) sb.Append(s);
        return sb.ToString();
    }

    // p(x) = arr[ceil(x/100 * n) - 1], 0-based, array sorted ascending
    private static long Percentile(long[] sorted, int x)
    {
        long n = sorted.Length;
        long idx = (x * n + 99L) / 100L - 1L;
        if (idx < 0) idx = 0;
        return sorted[(int)idx];
    }

    private static void JsonStr(StringBuilder sb, string s)
    {
        sb.Append('"');
        foreach (char c in s)
        {
            if (c == '"' || c == '\\')
            {
                sb.Append('\\').Append(c);
            }
            else if (c < 0x20)
            {
                sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
            }
            else
            {
                sb.Append(c);
            }
        }
        sb.Append('"');
    }

    private sealed class Worker
    {
        private readonly int _idx;
        // Events mode: pre-sized to BENCH_TXNS_PER_THREAD. Duration mode: grows
        // dynamically (initial capacity a few thousand); one entry per timed txn.
        public readonly List<long> Latencies;
        public long Retries;              // retryable-error count (timed window only)
        public readonly Thread Thread;

        // 31-bit LCG per SPEC.md, one independent stream per worker.
        private long _lcgState;

        private MySqlConnection _conn = null!;

        // BEGIN / COMMIT / ROLLBACK are plain (non-prepared) statements.
        private MySqlCommand _begin = null!;
        private MySqlCommand _commit = null!;
        private MySqlCommand _rollback = null!;

        // 9 distinct SQL texts, each server-side prepared once per worker.
        // Parameter objects are created once with an explicit MySqlDbType and only
        // their .Value is mutated per execution (type must stay stable once prepared).
        private MySqlCommand _pointSel = null!;
        private MySqlParameter _pointId = null!;
        private MySqlCommand _rangeSel = null!;
        private MySqlParameter _rangeLo = null!, _rangeHi = null!;
        private MySqlCommand _sumSel = null!;
        private MySqlParameter _sumLo = null!, _sumHi = null!;
        private MySqlCommand _orderSel = null!;
        private MySqlParameter _orderLo = null!, _orderHi = null!;
        private MySqlCommand _distinctSel = null!;
        private MySqlParameter _distinctLo = null!, _distinctHi = null!;
        private MySqlCommand _idxUpd = null!;
        private MySqlParameter _idxUpdId = null!;
        private MySqlCommand _nonIdxUpd = null!;
        private MySqlParameter _nonIdxUpdC = null!, _nonIdxUpdId = null!;
        private MySqlCommand _del = null!;
        private MySqlParameter _delId = null!;
        private MySqlCommand _ins = null!;
        private MySqlParameter _insId = null!, _insK = null!, _insC = null!, _insPad = null!;

        public Worker(int idx)
        {
            _idx = idx;
            Latencies = new List<long>(_durationS > 0 ? 4096 : _txnsPerThread);
            _lcgState = (long)_seed + idx;
            Thread = new Thread(Run) { Name = "bench-worker-" + idx, IsBackground = false };
        }

        private long Rand(long n) // uniform 0..n-1
        {
            _lcgState = (_lcgState * 1103515245L + 12345L) % 2147483648L; // 2^31
            return _lcgState % n;
        }

        private void Run()
        {
            try
            {
                _conn = new MySqlConnection(_connString);
                _conn.Open();

                _begin = new MySqlCommand("BEGIN", _conn);
                _commit = new MySqlCommand("COMMIT", _conn);
                _rollback = new MySqlCommand("ROLLBACK", _conn);

                _pointSel = new MySqlCommand("SELECT c FROM sbtest WHERE id = @id", _conn);
                _pointId = _pointSel.Parameters.Add("@id", MySqlDbType.Int64);
                _pointSel.Prepare();

                _rangeSel = new MySqlCommand("SELECT c FROM sbtest WHERE id BETWEEN @lo AND @hi", _conn);
                _rangeLo = _rangeSel.Parameters.Add("@lo", MySqlDbType.Int64);
                _rangeHi = _rangeSel.Parameters.Add("@hi", MySqlDbType.Int64);
                _rangeSel.Prepare();

                _sumSel = new MySqlCommand("SELECT SUM(k) FROM sbtest WHERE id BETWEEN @lo AND @hi", _conn);
                _sumLo = _sumSel.Parameters.Add("@lo", MySqlDbType.Int64);
                _sumHi = _sumSel.Parameters.Add("@hi", MySqlDbType.Int64);
                _sumSel.Prepare();

                _orderSel = new MySqlCommand("SELECT c FROM sbtest WHERE id BETWEEN @lo AND @hi ORDER BY c", _conn);
                _orderLo = _orderSel.Parameters.Add("@lo", MySqlDbType.Int64);
                _orderHi = _orderSel.Parameters.Add("@hi", MySqlDbType.Int64);
                _orderSel.Prepare();

                _distinctSel = new MySqlCommand("SELECT DISTINCT c FROM sbtest WHERE id BETWEEN @lo AND @hi ORDER BY c", _conn);
                _distinctLo = _distinctSel.Parameters.Add("@lo", MySqlDbType.Int64);
                _distinctHi = _distinctSel.Parameters.Add("@hi", MySqlDbType.Int64);
                _distinctSel.Prepare();

                _idxUpd = new MySqlCommand("UPDATE sbtest SET k = k + 1 WHERE id = @id", _conn);
                _idxUpdId = _idxUpd.Parameters.Add("@id", MySqlDbType.Int64);
                _idxUpd.Prepare();

                _nonIdxUpd = new MySqlCommand("UPDATE sbtest SET c = @c WHERE id = @id", _conn);
                _nonIdxUpdC = _nonIdxUpd.Parameters.Add("@c", MySqlDbType.VarChar);
                _nonIdxUpdId = _nonIdxUpd.Parameters.Add("@id", MySqlDbType.Int64);
                _nonIdxUpd.Prepare();

                _del = new MySqlCommand("DELETE FROM sbtest WHERE id = @id", _conn);
                _delId = _del.Parameters.Add("@id", MySqlDbType.Int64);
                _del.Prepare();

                _ins = new MySqlCommand("INSERT INTO sbtest (id, k, c, pad) VALUES (@id, @k, @c, @pad)", _conn);
                _insId = _ins.Parameters.Add("@id", MySqlDbType.Int64);
                _insK = _ins.Parameters.Add("@k", MySqlDbType.Int64);
                _insC = _ins.Parameters.Add("@c", MySqlDbType.VarChar);
                _insPad = _ins.Parameters.Add("@pad", MySqlDbType.VarChar);
                _ins.Prepare();

                for (int i = 0; i < _warmupTxns; i++)
                {
                    RunTxnWithRetries(timed: false);
                }
                // Retries is only incremented during the timed window, so warmup
                // retries are never reported (counter effectively reset after warmup).

                _startBarrier.SignalAndWait(); // released together with main's t_start sample

                long freq = Stopwatch.Frequency;
                if (_durationS > 0)
                {
                    // Duration mode: deadline checked before each new transaction;
                    // a transaction in flight at the deadline runs to successful
                    // commit (its retries included).
                    long start = Stopwatch.GetTimestamp();
                    long deadlineTicks = (long)_durationS * freq;
                    while (Stopwatch.GetTimestamp() - start < deadlineTicks)
                    {
                        long t0 = Stopwatch.GetTimestamp();
                        RunTxnWithRetries(timed: true);
                        long t1 = Stopwatch.GetTimestamp();
                        Latencies.Add((t1 - t0) * 1_000_000L / freq); // integer microseconds, truncated; retries included
                    }
                }
                else
                {
                    // Events mode: exactly BENCH_TXNS_PER_THREAD timed transactions.
                    for (int i = 0; i < _txnsPerThread; i++)
                    {
                        long t0 = Stopwatch.GetTimestamp();
                        RunTxnWithRetries(timed: true);
                        long t1 = Stopwatch.GetTimestamp();
                        Latencies.Add((t1 - t0) * 1_000_000L / freq); // integer microseconds, truncated; retries included
                    }
                }

                _conn.Close();
            }
            catch (Exception e)
            {
                // Any non-retryable failure anywhere (connect, prepare, txn, barrier):
                // report and hard-exit so the process never hangs on the barrier.
                Console.Error.WriteLine("worker " + _idx + " error: " + e);
                Environment.Exit(1);
            }
        }

        // Runs one transaction, retrying on deadlock (1213), lock wait timeout (1205)
        // and duplicate key (1062) with all values redrawn (the PRNG stream continues).
        private void RunTxnWithRetries(bool timed)
        {
            for (;;)
            {
                try
                {
                    TxnOnce();
                    return;
                }
                catch (MySqlException e)
                {
                    int code = (int)e.ErrorCode;
                    if (code == 1213 || code == 1205 || code == 1062)
                    {
                        try
                        {
                            _rollback.ExecuteNonQuery();
                        }
                        catch (Exception)
                        {
                            // rollback errors are ignored per spec
                        }
                        if (timed) Retries++;
                        // loop: re-run the whole transaction with fresh draws
                    }
                    else
                    {
                        throw;
                    }
                }
            }
        }

        private static void FetchAll(MySqlCommand cmd)
        {
            using (MySqlDataReader reader = cmd.ExecuteReader())
            {
                while (reader.Read())
                {
                    _ = reader.GetValue(0); // fetch every row; GetValue is DECIMAL/NULL-safe for SUM(k)
                }
            } // reader disposed before the next command on this connection
        }

        // One sysbench oltp_read_write transaction; values drawn in execution order.
        private void TxnOnce()
        {
            _begin.ExecuteNonQuery();

            for (int i = 0; i < 10; i++)
            {
                _pointId.Value = 1L + Rand(_tableRows);
                FetchAll(_pointSel);
            }

            long lo = 1L + Rand(_tableRows);
            _rangeLo.Value = lo;
            _rangeHi.Value = lo + _rangeSize - 1;
            FetchAll(_rangeSel);

            lo = 1L + Rand(_tableRows);
            _sumLo.Value = lo;
            _sumHi.Value = lo + _rangeSize - 1;
            FetchAll(_sumSel);

            lo = 1L + Rand(_tableRows);
            _orderLo.Value = lo;
            _orderHi.Value = lo + _rangeSize - 1;
            FetchAll(_orderSel);

            lo = 1L + Rand(_tableRows);
            _distinctLo.Value = lo;
            _distinctHi.Value = lo + _rangeSize - 1;
            FetchAll(_distinctSel);

            _idxUpdId.Value = 1L + Rand(_tableRows);
            _idxUpd.ExecuteNonQuery();

            _nonIdxUpdC.Value = _cVal;
            _nonIdxUpdId.Value = 1L + Rand(_tableRows);
            _nonIdxUpd.ExecuteNonQuery();

            long id = 1L + Rand(_tableRows);
            long k = Rand(_tableRows);
            _delId.Value = id;
            _del.ExecuteNonQuery();
            _insId.Value = id;
            _insK.Value = k;
            _insC.Value = _cVal;
            _insPad.Value = _padVal;
            _ins.ExecuteNonQuery();

            _commit.ExecuteNonQuery();
        }
    }

    private static int Main()
    {
        _dbHost = EnvStr("MYSQL_HOST");
        string port = EnvStr("MYSQL_PORT");
        string user = EnvStr("MYSQL_USER");
        string password = EnvStr("MYSQL_PASSWORD");
        string db = EnvStr("MYSQL_DB");

        _tableRows = EnvInt("BENCH_TABLE_ROWS");
        _threads = EnvInt("BENCH_THREADS");
        _warmupTxns = EnvInt("BENCH_WARMUP_TXNS");
        _txnsPerThread = EnvInt("BENCH_TXNS_PER_THREAD");
        _durationS = EnvIntOpt("BENCH_DURATION_S", 0); // 0 = events mode (default)
        _rangeSize = EnvInt("BENCH_RANGE_SIZE");
        _seed = EnvInt("BENCH_SEED");

        _cVal = Repeat("abcdefghij", 12);  // 120 chars
        _padVal = Repeat("0123456789", 6); // 60 chars

        var csb = new MySqlConnectionStringBuilder
        {
            Server = _dbHost,
            Port = uint.Parse(port.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture),
            UserID = user,
            Password = password,
            Database = db,
            Pooling = false,                 // each worker owns one dedicated connection
            SslMode = MySqlSslMode.None,     // TLS disabled per SPEC
            AllowPublicKeyRetrieval = true,  // caching_sha2_password over plaintext
            DefaultCommandTimeout = 0,       // no client-side timeout (lock waits surface as server error 1205)
        };
        _connString = csb.ConnectionString;

        _startBarrier = new Barrier(_threads + 1);

        var workers = new Worker[_threads];
        for (int i = 0; i < _threads; i++)
        {
            workers[i] = new Worker(i);
            workers[i].Thread.Start();
        }

        _startBarrier.SignalAndWait(); // all workers connected, prepared, warmed up
        long tStart = Stopwatch.GetTimestamp();
        for (int i = 0; i < _threads; i++)
        {
            workers[i].Thread.Join();
        }
        long tEnd = Stopwatch.GetTimestamp();

        // txns = total committed timed transactions across workers. In events mode
        // this equals BENCH_THREADS * BENCH_TXNS_PER_THREAD; in duration mode each
        // worker contributes however many transactions it actually committed.
        long txns = 0;
        long totalRetries = 0;
        for (int i = 0; i < _threads; i++)
        {
            txns += workers[i].Latencies.Count;
            totalRetries += workers[i].Retries;
        }
        var all = new long[txns];
        int pos = 0;
        for (int i = 0; i < _threads; i++)
        {
            workers[i].Latencies.CopyTo(all, pos);
            pos += workers[i].Latencies.Count;
        }
        Array.Sort(all);

        double elapsedS = (tEnd - tStart) / (double)Stopwatch.Frequency;
        double tps = txns / elapsedS;
        double qps = tps * 20.0; // 18 statements + BEGIN + COMMIT, sysbench counting

        long p50 = Percentile(all, 50);
        long p95 = Percentile(all, 95);
        long p99 = Percentile(all, 99);
        long maxUs = all[all.Length - 1];

        string clientHost;
        try
        {
            clientHost = Environment.MachineName;
            if (string.IsNullOrEmpty(clientHost)) clientHost = Dns.GetHostName();
        }
        catch (Exception)
        {
            clientHost = "unknown";
        }

        var sb = new StringBuilder(512);
        sb.Append("{\"lang\":\"csharp\"");
        sb.Append(",\"driver\":\"MySqlConnector\"");
        sb.Append(",\"driver_version\":");
        JsonStr(sb, DriverVersion);
        sb.Append(",\"runtime_version\":");
        JsonStr(sb, Environment.Version.ToString());
        sb.Append(",\"prepared\":true");
        sb.Append(",\"client_host\":");
        JsonStr(sb, clientHost);
        sb.Append(",\"db_host\":");
        JsonStr(sb, _dbHost);
        sb.Append(",\"threads\":").Append(_threads.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"txns\":").Append(txns.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"retries\":").Append(totalRetries.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"errors\":0");
        sb.Append(",\"elapsed_s\":").Append(elapsedS.ToString("F3", CultureInfo.InvariantCulture));
        sb.Append(",\"tps\":").Append(tps.ToString("F1", CultureInfo.InvariantCulture));
        sb.Append(",\"qps\":").Append(qps.ToString("F1", CultureInfo.InvariantCulture));
        sb.Append(",\"p50_us\":").Append(p50.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"p95_us\":").Append(p95.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"p99_us\":").Append(p99.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"max_us\":").Append(maxUs.ToString(CultureInfo.InvariantCulture));
        sb.Append('}');

        Console.Out.WriteLine(sb.ToString());
        return 0;
    }
}
