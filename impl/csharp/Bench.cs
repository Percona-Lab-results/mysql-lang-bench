// C# implementation of the v3 four micro-workload benchmark (see SPEC.md).
// Driver: MySqlConnector. One dedicated OS thread + one dedicated MySqlConnection
// per worker (no pooling/sharing), server-side prepared statements.
//
// Workload is selected by BENCH_WORKLOAD (1..4):
//   1 int-insert  : prepared multi-row INSERT of BENCH_INT_BATCH ints into bench_int
//   2 int-read    : SELECT val FROM bench_int LIMIT n, sum in app code
//   3 blob-insert : fill BENCH_BLOB_SIZE random bytes, MD5 them, INSERT into bench_blob
//   4 blob-read   : SELECT data WHERE id=?, MD5 the content in app code
//
// GC mode (stock workstation vs tuned server GC) is controlled at runtime by the
// DOTNET_gcServer environment variable, which run.sh sets from BENCH_TUNED. The
// csproj therefore does NOT pin ServerGarbageCollection.

using System;
using System.Buffers.Binary;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net;
using System.Security.Cryptography;
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
    private static int _workload;
    private static int _threads;
    private static long _seed;
    private static int _intRows;
    private static int _intBatch;
    private static int _blobCount;
    private static int _blobSize;

    private static Barrier _startBarrier = null!;

    // Black-box sink: app-computed sums and MD5 digests are funnelled here with
    // Interlocked.Add (a side effect the JIT cannot elide) so the read/hash work
    // is never optimized away.
    private static long _sink;

    // Per-worker completed-operation counters (rows for WL1/2, blobs for WL3/4).
    // Each worker writes only its own slot via Volatile.Write; the progress
    // monitor sums them with Volatile.Read. No locking.
    private static long[] _opsSlots = Array.Empty<long>();

    // ---- progress checkpoint (BENCH_PROGRESS_FILE / BENCH_PROGRESS_SEC) ----
    private static string? _progressFile;
    private static int _progressSec = 5;
    private static long _tStart; // Stopwatch timestamp at barrier release
    private static readonly ManualResetEventSlim _stopProgress = new ManualResetEventSlim(false);

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

    private static string WorkloadName(int w) => w switch
    {
        1 => "int-insert",
        2 => "int-read",
        3 => "blob-insert",
        4 => "blob-read",
        _ => "unknown",
    };

    private static string Unit(int w) => (w == 1 || w == 2) ? "rows/s" : "blobs/s";

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
        public readonly List<long> Latencies; // one entry per timed operation
        public readonly Thread Thread;

        // 31-bit LCG per SPEC.md, one independent stream per worker.
        private long _lcgState;

        private MySqlConnection _conn = null!;

        public Worker(int idx, int capacity)
        {
            _idx = idx;
            Latencies = new List<long>(capacity);
            _lcgState = _seed + idx;
            Thread = new Thread(Run) { Name = "bench-worker-" + idx, IsBackground = false };
        }

        // next(): state = (state*1103515245 + 12345) mod 2^31; return state.
        // Returns a value in [0, 2^31 - 1] (fits a non-negative signed 32-bit int).
        private long Next()
        {
            _lcgState = (_lcgState * 1103515245L + 12345L) % 2147483648L; // 2^31
            return _lcgState;
        }

        private void Run()
        {
            try
            {
                _conn = new MySqlConnection(_connString);
                _conn.Open();

                switch (_workload)
                {
                    case 1: RunIntInsert(); break;
                    case 2: RunIntRead(); break;
                    case 3: RunBlobInsert(); break;
                    case 4: RunBlobRead(); break;
                    default:
                        Console.Error.WriteLine("invalid BENCH_WORKLOAD: " + _workload);
                        Environment.Exit(1);
                        break;
                }

                _conn.Close();
            }
            catch (Exception e)
            {
                // Any failure anywhere (connect, prepare, execute, barrier): report
                // and hard-exit so the process never hangs on the start barrier.
                Console.Error.WriteLine("worker " + _idx + " error: " + e);
                Environment.Exit(1);
            }
        }

        private void Publish(long opsDone) => Volatile.Write(ref _opsSlots[_idx], opsDone);

        private static long Micros(long t0, long t1) =>
            (t1 - t0) * 1_000_000L / Stopwatch.Frequency;

        // ---- Workload 1: prepared multi-row INSERT of BENCH_INT_BATCH ints ----
        private void RunIntInsert()
        {
            // INSERT INTO bench_int (val) VALUES (@v0),(@v1),...,(@v{batch-1})
            var sql = new StringBuilder("INSERT INTO bench_int (val) VALUES ");
            for (int b = 0; b < _intBatch; b++)
            {
                if (b > 0) sql.Append(',');
                sql.Append("(@v").Append(b.ToString(CultureInfo.InvariantCulture)).Append(')');
            }
            using var cmd = new MySqlCommand(sql.ToString(), _conn);
            var ps = new MySqlParameter[_intBatch];
            for (int b = 0; b < _intBatch; b++)
                ps[b] = cmd.Parameters.Add("@v" + b.ToString(CultureInfo.InvariantCulture), MySqlDbType.Int32);
            cmd.Prepare();

            int iters = _intRows / _intBatch;

            _startBarrier.SignalAndWait();

            // Autocommit ON: each multi-row INSERT is its own commit, no wrapping
            // transaction (per SPEC; the server does not fsync per commit).
            long rows = 0;
            for (int it = 0; it < iters; it++)
            {
                for (int b = 0; b < _intBatch; b++)
                    ps[b].Value = (int)Next(); // SetInt32 from next()
                long t0 = Stopwatch.GetTimestamp();
                cmd.ExecuteNonQuery();
                long t1 = Stopwatch.GetTimestamp();
                Latencies.Add(Micros(t0, t1));
                rows += _intBatch;
                Publish(rows);
            }
            Publish(rows);
        }

        // ---- Workload 2: SELECT val FROM bench_int LIMIT n, sum in app code ----
        private void RunIntRead()
        {
            // LIMIT is a trusted integer from env, interpolated into the SQL text.
            string sql = "SELECT val FROM bench_int LIMIT " +
                         _intRows.ToString(CultureInfo.InvariantCulture);
            using var cmd = new MySqlCommand(sql, _conn);
            cmd.Prepare();

            _startBarrier.SignalAndWait();

            long sum = 0;
            long rows = 0;
            long t0 = Stopwatch.GetTimestamp();
            using (MySqlDataReader reader = cmd.ExecuteReader())
            {
                while (reader.Read())
                {
                    sum += reader.GetInt32(0);
                    rows++;
                    Publish(rows);
                }
            }
            long t1 = Stopwatch.GetTimestamp();
            Latencies.Add(Micros(t0, t1)); // one sample for the whole scan

            Interlocked.Add(ref _sink, sum); // black-box sink
            Publish(rows);
        }

        // ---- Workload 3: fill random blob, MD5 it, INSERT (id, data) ----
        private void RunBlobInsert()
        {
            using var cmd = new MySqlCommand(
                "INSERT INTO bench_blob (id, data) VALUES (@id, @data)", _conn);
            MySqlParameter pId = cmd.Parameters.Add("@id", MySqlDbType.Int64);
            MySqlParameter pData = cmd.Parameters.Add("@data", MySqlDbType.LongBlob);
            cmd.Prepare();

            // Fill the BENCH_BLOB_SIZE buffer ONCE per worker with a fast bulk
            // random fill (not a per-byte interpreted loop, per SPEC). Deterministic
            // per worker; content need not match other languages.
            byte[] data = new byte[_blobSize];
            new Random(unchecked((int)(_seed + _idx))).NextBytes(data);

            using MD5 md5 = MD5.Create(); // one instance per worker
            long digestSink = 0;

            _startBarrier.SignalAndWait();

            // Autocommit ON: each blob INSERT is its own commit, no wrapping
            // transaction (same rule as workload 1).
            long blobs = 0;
            for (int k = 0; k < _blobCount; k++)
            {
                long id = (long)_idx * _blobCount + k;
                // Overwrite the first 8 bytes with the id so each blob's content
                // (and its MD5) differs; MD5 still hashes the full buffer.
                BinaryPrimitives.WriteInt64LittleEndian(data.AsSpan(0, 8), id);
                byte[] digest = md5.ComputeHash(data);
                digestSink += digest[0];

                pId.Value = id;
                pData.Value = data;
                long t0 = Stopwatch.GetTimestamp();
                cmd.ExecuteNonQuery();
                long t1 = Stopwatch.GetTimestamp();
                Latencies.Add(Micros(t0, t1));
                blobs++;
                Publish(blobs);
            }

            Interlocked.Add(ref _sink, digestSink);
            Publish(blobs);
        }

        // ---- Workload 4: SELECT data WHERE id=?, MD5 the content in app code ----
        private void RunBlobRead()
        {
            using var cmd = new MySqlCommand(
                "SELECT data FROM bench_blob WHERE id = @id", _conn);
            MySqlParameter pId = cmd.Parameters.Add("@id", MySqlDbType.Int64);
            cmd.Prepare();

            using MD5 md5 = MD5.Create(); // one instance per worker
            long digestSink = 0;

            _startBarrier.SignalAndWait();

            long blobs = 0;
            for (int k = 0; k < _blobCount; k++)
            {
                pId.Value = (long)_idx * _blobCount + k;
                long t0 = Stopwatch.GetTimestamp();
                byte[] data;
                using (MySqlDataReader reader = cmd.ExecuteReader())
                {
                    if (!reader.Read())
                    {
                        Console.Error.WriteLine("worker " + _idx + " missing blob id " +
                            ((long)_idx * _blobCount + k).ToString(CultureInfo.InvariantCulture));
                        Environment.Exit(1);
                    }
                    data = reader.GetFieldValue<byte[]>(0);
                }
                long t1 = Stopwatch.GetTimestamp();
                Latencies.Add(Micros(t0, t1));

                byte[] digest = md5.ComputeHash(data);
                digestSink += digest[0];
                blobs++;
                Publish(blobs);
            }

            Interlocked.Add(ref _sink, digestSink);
            Publish(blobs);
        }
    }

    // Sum all per-worker slots (completed operations so far).
    private static long OpsSoFar()
    {
        long total = 0;
        for (int i = 0; i < _opsSlots.Length; i++)
            total += Volatile.Read(ref _opsSlots[i]);
        return total;
    }

    // Atomically overwrite the progress file: write FILE.tmp, then rename over FILE.
    private static void WriteProgress(string file)
    {
        long ops = OpsSoFar();
        double elapsed = (Stopwatch.GetTimestamp() - _tStart) / (double)Stopwatch.Frequency;
        double throughput = elapsed > 0 ? ops / elapsed : 0.0;

        var sb = new StringBuilder(256);
        sb.Append("{\"workload\":").Append(_workload.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"workload_name\":");
        JsonStr(sb, WorkloadName(_workload));
        sb.Append(",\"unit\":");
        JsonStr(sb, Unit(_workload));
        sb.Append(",\"threads\":").Append(_threads.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"ops\":").Append(ops.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"elapsed_s\":").Append(elapsed.ToString("F3", CultureInfo.InvariantCulture));
        sb.Append(",\"throughput\":").Append(throughput.ToString("F1", CultureInfo.InvariantCulture));
        sb.Append(",\"partial\":true}");

        string tmp = file + ".tmp";
        try
        {
            File.WriteAllText(tmp, sb.ToString());
            File.Move(tmp, file, overwrite: true); // atomic rename on same filesystem
        }
        catch (Exception)
        {
            // progress is best-effort; never let a checkpoint failure abort the run
        }
    }

    private static void ProgressLoop(string file)
    {
        int ms = _progressSec * 1000;
        if (ms <= 0) ms = 5000;
        while (!_stopProgress.Wait(ms))
        {
            WriteProgress(file);
        }
    }

    private static int Main()
    {
        _dbHost = EnvStr("MYSQL_HOST");
        string port = EnvStr("MYSQL_PORT");
        string user = EnvStr("MYSQL_USER");
        string password = EnvStr("MYSQL_PASSWORD");
        string db = EnvStr("MYSQL_DB");

        _workload = EnvInt("BENCH_WORKLOAD");
        _threads = EnvInt("BENCH_THREADS");
        _seed = EnvInt("BENCH_SEED");
        _intRows = EnvIntOpt("BENCH_INT_ROWS", 1000000);
        _intBatch = EnvIntOpt("BENCH_INT_BATCH", 10);
        _blobCount = EnvIntOpt("BENCH_BLOB_COUNT", 100);
        _blobSize = EnvIntOpt("BENCH_BLOB_SIZE", 1048576);

        _progressFile = Environment.GetEnvironmentVariable("BENCH_PROGRESS_FILE");
        if (string.IsNullOrEmpty(_progressFile)) _progressFile = null;
        _progressSec = EnvIntOpt("BENCH_PROGRESS_SEC", 5);

        if (_workload < 1 || _workload > 4)
        {
            Console.Error.WriteLine("BENCH_WORKLOAD must be 1..4, got " + _workload);
            return 1;
        }
        if (_workload == 1 && _intBatch > 0 && _intRows % _intBatch != 0)
        {
            Console.Error.WriteLine("BENCH_INT_ROWS (" + _intRows +
                ") must be a multiple of BENCH_INT_BATCH (" + _intBatch + ")");
            return 1;
        }

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
            DefaultCommandTimeout = 0,       // no client-side timeout on big scans/inserts
        };
        _connString = csb.ConnectionString;

        _startBarrier = new Barrier(_threads + 1);
        _opsSlots = new long[_threads];

        // Per-worker latency capacity: exactly the number of timed operations.
        int capacity = _workload switch
        {
            1 => (_intBatch > 0 ? _intRows / _intBatch : 0),
            2 => 1,
            3 => _blobCount,
            4 => _blobCount,
            _ => 0,
        };

        var workers = new Worker[_threads];
        for (int i = 0; i < _threads; i++)
        {
            workers[i] = new Worker(i, capacity);
            workers[i].Thread.Start();
        }

        _startBarrier.SignalAndWait(); // all workers connected + prepared
        _tStart = Stopwatch.GetTimestamp();

        Thread? monitor = null;
        if (_progressFile is not null)
        {
            string pf = _progressFile;
            monitor = new Thread(() => ProgressLoop(pf))
            {
                Name = "bench-progress",
                IsBackground = true,
            };
            monitor.Start();
        }

        for (int i = 0; i < _threads; i++)
            workers[i].Thread.Join();
        long tEnd = Stopwatch.GetTimestamp();

        if (monitor is not null)
        {
            _stopProgress.Set();
            monitor.Join();
        }

        // ops = total completed operations across workers (rows for WL1/2, blobs WL3/4).
        long ops = 0;
        long nSamples = 0;
        for (int i = 0; i < _threads; i++)
        {
            ops += Volatile.Read(ref _opsSlots[i]);
            nSamples += workers[i].Latencies.Count;
        }

        var all = new long[nSamples];
        int pos = 0;
        for (int i = 0; i < _threads; i++)
        {
            workers[i].Latencies.CopyTo(all, pos);
            pos += workers[i].Latencies.Count;
        }
        Array.Sort(all);

        double elapsedS = (tEnd - _tStart) / (double)Stopwatch.Frequency;
        double throughput = elapsedS > 0 ? ops / elapsedS : 0.0;
        double mbPerS = 0.0;
        if (_workload == 3 || _workload == 4)
            mbPerS = elapsedS > 0 ? ops * (double)_blobSize / 1e6 / elapsedS : 0.0;

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

        var sb = new StringBuilder(640);
        sb.Append("{\"lang\":\"csharp\"");
        sb.Append(",\"workload\":").Append(_workload.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"workload_name\":");
        JsonStr(sb, WorkloadName(_workload));
        sb.Append(",\"unit\":");
        JsonStr(sb, Unit(_workload));
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
        sb.Append(",\"ops\":").Append(ops.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"errors\":0");
        sb.Append(",\"elapsed_s\":").Append(elapsedS.ToString("F3", CultureInfo.InvariantCulture));
        sb.Append(",\"throughput\":").Append(throughput.ToString("F1", CultureInfo.InvariantCulture));
        sb.Append(",\"mb_per_s\":").Append(mbPerS.ToString("F1", CultureInfo.InvariantCulture));
        sb.Append(",\"p50_us\":").Append(p50.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"p95_us\":").Append(p95.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"p99_us\":").Append(p99.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"max_us\":").Append(maxUs.ToString(CultureInfo.InvariantCulture));
        sb.Append(",\"client_cpu_s\":0");
        sb.Append(",\"server_cpu_s\":0");
        sb.Append('}');

        Console.Out.WriteLine(sb.ToString());

        // Final JSON is on stdout; the progress file is now superseded -> delete it.
        if (_progressFile is not null)
        {
            try { File.Delete(_progressFile); } catch (Exception) { }
        }

        // Touch the sink so the read/hash work is observably used.
        if (_sink == long.MinValue) Console.Error.Write("");

        return 0;
    }
}
