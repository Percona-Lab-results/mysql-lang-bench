<?php
// MySQL language benchmark - PHP implementation (v3: four micro-workloads).
//
// mysqli + server-side prepared statements. PHP has no threads, so the
// BENCH_THREADS workers are forked processes (pcntl_fork); every child owns its
// own connection (created AFTER the fork, per SPEC) and its own 31-bit LCG PRNG
// seeded BENCH_SEED + worker_index. A UNIX stream socketpair per child is the
// start barrier. Each child writes its per-op latencies (integer microseconds,
// one per line) to a temp file; the parent merges them and prints one JSON line.
//
// Parent/child protocol:
//   - a per-worker UNIX stream socketpair is created before each fork;
//   - the child connects, prepares its statement(s), writes "R" (ready) to its
//     socket and blocks reading one byte (the start barrier);
//   - the parent waits for all "R" bytes, samples t_start, writes "S" to every
//     child, then waits for all children (non-blocking waitpid + poll when
//     progress is enabled, blocking waitpid otherwise), samples t_end;
//   - each child writes its latencies to sys_get_temp_dir()/bench-php-<pid>.result
//     and the parent merges those files into the single JSON result line.
//
// Intermediate progress (SPEC "Intermediate progress"): when BENCH_PROGRESS_FILE
// is set, each child writes its completed-op count to a per-child temp
// (BENCH_PROGRESS_FILE.w<i>) roughly every BENCH_PROGRESS_SEC seconds; the parent
// polls, sums the child temps and atomically rewrites BENCH_PROGRESS_FILE with a
// partial JSON line. On success the parent deletes BENCH_PROGRESS_FILE and the
// child temps; on failure it leaves BENCH_PROGRESS_FILE behind for recovery.

mysqli_report(MYSQLI_REPORT_ERROR | MYSQLI_REPORT_STRICT); // SQL errors -> exceptions
ini_set('memory_limit', '-1'); // the parent may merge tens of millions of latency samples

// ---- env helpers ----
function env_str(string $name): string {
    $v = getenv($name);
    if ($v === false || $v === '') {
        throw new RuntimeException("missing required environment variable: $name");
    }
    return $v;
}

function env_int(string $name): int {
    return (int) env_str($name);
}

function env_int_opt(string $name, int $default): int {
    $v = getenv($name);
    if ($v === false || $v === '') {
        return $default;
    }
    return (int) $v;
}

// ---- PRNG: 31-bit LCG. One independent stream per worker process: the child
// seeds this with BENCH_SEED + worker_index after the fork.
// Validation (seed 42, worker 0): first next() = 1250496027. ----
$lcg_state = 0;

function lcg_next(): int {
    global $lcg_state;
    // PHP ints are 64-bit: state < 2^31, so state * 1103515245 + 12345 < 2^63.
    $lcg_state = ($lcg_state * 1103515245 + 12345) % 2147483648;
    return $lcg_state;
}

// ---- workload metadata: [workload_name, unit] ----
const WL_META = [
    1 => ['int-insert',  'rows/s'],
    2 => ['int-read',    'rows/s'],
    3 => ['blob-insert', 'blobs/s'],
    4 => ['blob-read',   'blobs/s'],
];

// Black-box sink: workloads feed computed results here so they are not dead code.
$g_sink = 0;

function result_file_path(int $pid): string {
    return sys_get_temp_dir() . '/bench-php-' . $pid . '.result';
}

// Atomically publish a child's completed-op count (write tmp, rename over FILE).
function write_child_progress(string $path, int $count): void {
    $tmp = $path . '.tmp';
    if (@file_put_contents($tmp, (string) $count) !== false) {
        @rename($tmp, $path);
    }
}

// ---- Worker body: runs in the forked child only. ----
function worker_main(int $idx, $sock, array $cfg): void {
    global $lcg_state, $g_sink;
    $lcg_state = $cfg['seed'] + $idx;

    $W = $cfg['workload'];
    $progress = $cfg['progressFile'] !== '';
    $childProg = $progress ? $cfg['progressFile'] . '.w' . $idx : '';
    $intervalNs = $cfg['progressSec'] * 1000000000;

    // Connection (child only, after fork): TCP, autocommit ON (mysqli default).
    $conn = new mysqli($cfg['host'], $cfg['user'], $cfg['password'], $cfg['db'], $cfg['port']);

    // ---- Prepare statement(s) for this workload (outside the timed window). ----
    switch ($W) {
        case 1:
            $batch = $cfg['intBatch'];
            $sql = 'INSERT INTO bench_int (val) VALUES '
                 . implode(',', array_fill(0, $batch, '(?)'));
            $stmt = $conn->prepare($sql);
            // bind_param binds by reference: bind once, mutate per execute. The
            // reference array is the documented way to bind N dynamic params.
            $vals = array_fill(0, $batch, 0);
            $bindArgs = [str_repeat('i', $batch)];
            for ($k = 0; $k < $batch; $k++) {
                $bindArgs[] = &$vals[$k];
            }
            call_user_func_array([$stmt, 'bind_param'], $bindArgs);
            break;
        case 2:
            // LIMIT takes a config integer (not user input): embed it literally.
            $stmt = $conn->prepare('SELECT val FROM bench_int LIMIT ' . (int) $cfg['intRows']);
            break;
        case 3:
            $stmt = $conn->prepare('INSERT INTO bench_blob (id, data) VALUES (?, ?)');
            $id = 0;
            $data = '';
            $stmt->bind_param('is', $id, $data);
            break;
        case 4:
            $stmt = $conn->prepare('SELECT data FROM bench_blob WHERE id = ?');
            $id = 0;
            $stmt->bind_param('i', $id);
            break;
        default:
            throw new RuntimeException("invalid BENCH_WORKLOAD: $W");
    }

    // ---- Barrier: signal ready, block until the parent sends the start byte. ----
    if (fwrite($sock, 'R') !== 1) {
        throw new RuntimeException('failed to write ready byte to parent');
    }
    $start = fread($sock, 1); // blocking read: this is the start barrier
    if ($start !== 'S') {
        throw new RuntimeException('did not receive start signal from parent');
    }

    // ---- Timed run. One latency sample (integer microseconds) per DB op. ----
    $latencies = [];
    $completed = 0;
    $nextProg = $progress ? hrtime(true) + $intervalNs : PHP_INT_MAX;

    switch ($W) {
        case 1: // integer insert: prepared multi-row INSERT, BATCH rows each.
            // Autocommit ON (mysqli default): each INSERT is its own commit; no
            // wrapping BEGIN/COMMIT. Same commit strategy in every language.
            $batch = $cfg['intBatch'];
            $iters = intdiv($cfg['intRows'], $batch);
            for ($it = 0; $it < $iters; $it++) {
                for ($k = 0; $k < $batch; $k++) {
                    // next() is 0..2^31-1, i.e. already a valid signed 32-bit int.
                    $vals[$k] = lcg_next();
                }
                $t0 = hrtime(true);
                $stmt->execute();
                $latencies[] = intdiv(hrtime(true) - $t0, 1000);
                $completed += $batch;
                if ($progress && hrtime(true) >= $nextProg) {
                    write_child_progress($childProg, $completed);
                    $nextProg += $intervalNs;
                }
            }
            break;

        case 2: // integer fetch + sum: whole scan is one latency sample.
            $sum = 0; // 64-bit accumulator
            $cnt = 0;
            $t0 = hrtime(true);
            $stmt->execute();
            $res = $stmt->get_result();
            while (($row = $res->fetch_row()) !== null) {
                $sum += (int) $row[0];
                $cnt++;
                // Progress reflects rows read; check the clock sparsely.
                if ($progress && ($cnt & 0xFFFF) === 0 && hrtime(true) >= $nextProg) {
                    write_child_progress($childProg, $cnt);
                    $nextProg += $intervalNs;
                }
            }
            $res->free();
            $latencies[] = intdiv(hrtime(true) - $t0, 1000);
            $g_sink += $sum; // consume
            if ($progress) {
                write_child_progress($childProg, $cnt);
            }
            break;

        case 3: // blob insert + MD5: one buffer per worker, MD5 into sink, prepared INSERT.
            // Autocommit ON (mysqli default): each blob INSERT is its own commit.
            // Content must NOT be a per-byte interpreted loop: build one random
            // buffer once via a fast native facility, then before each blob
            // overwrite its first 8 bytes with the blob id so every blob (and
            // its MD5) differs; MD5 still hashes the full buffer each time.
            $count = $cfg['blobCount'];
            $size = $cfg['blobSize'];
            $data = $size > 0 ? random_bytes($size) : '';
            $idBytes = $size >= 8 ? 8 : $size; // bytes of id to stamp (buffer may be tiny)
            for ($k = 0; $k < $count; $k++) {
                $id = $idx * $count + $k;         // contiguous per-worker id range
                if ($idBytes > 0) {
                    // 8-byte little-endian id stamped over the buffer's head.
                    $data = substr_replace($data, substr(pack('P', $id), 0, $idBytes), 0, $idBytes);
                }
                $digest = md5($data, true);       // MD5 of the whole buffer, in app code
                $g_sink ^= ord($digest[0]);       // consume the digest
                $t0 = hrtime(true);
                $stmt->execute();
                $latencies[] = intdiv(hrtime(true) - $t0, 1000);
                $completed++;
                if ($progress && hrtime(true) >= $nextProg) {
                    write_child_progress($childProg, $completed);
                    $nextProg += $intervalNs;
                }
            }
            break;

        case 4: // blob read + MD5: fetch own blobs by id, MD5 the content.
            $count = $cfg['blobCount'];
            for ($k = 0; $k < $count; $k++) {
                $id = $idx * $count + $k;
                $t0 = hrtime(true);
                $stmt->execute();
                $res = $stmt->get_result();
                $row = $res->fetch_row();
                $res->free();
                $latencies[] = intdiv(hrtime(true) - $t0, 1000);
                if ($row === null) {
                    throw new RuntimeException("missing blob id=$id");
                }
                $digest = md5($row[0], true);     // MD5 of the content, in app code
                $g_sink ^= ord($digest[0]);       // consume the digest
                $completed++;
                if ($progress && hrtime(true) >= $nextProg) {
                    write_child_progress($childProg, $completed);
                    $nextProg += $intervalNs;
                }
            }
            break;
    }

    $conn->close();

    // Force the sink to be observable so the compiler/optimizer cannot elide it.
    if ($g_sink === PHP_INT_MIN) {
        fwrite(STDERR, "sink=$g_sink\n");
    }

    $path = result_file_path(getmypid());
    if (file_put_contents($path, implode("\n", $latencies) . "\n") === false) {
        throw new RuntimeException("failed to write result file: $path");
    }
}

// ---- Parent: atomically rewrite BENCH_PROGRESS_FILE with a partial result. ----
function write_parent_progress(array $cfg, int $W, int $threads, int $tStartNs, int $nowNs): void {
    $opsSoFar = 0;
    for ($i = 0; $i < $threads; $i++) {
        $c = @file_get_contents($cfg['progressFile'] . '.w' . $i);
        if ($c !== false) {
            $opsSoFar += (int) $c;
        }
    }
    $elapsed = ($nowNs - $tStartNs) / 1e9;
    $thr = $elapsed > 0 ? $opsSoFar / $elapsed : 0.0;
    $line = json_encode([
        'workload' => $W,
        'workload_name' => WL_META[$W][0],
        'unit' => WL_META[$W][1],
        'threads' => $threads,
        'ops' => $opsSoFar,
        'elapsed_s' => round($elapsed, 3),
        'throughput' => round($thr, 1),
        'partial' => true,
    ], JSON_PRESERVE_ZERO_FRACTION);
    $tmp = $cfg['progressFile'] . '.parent.tmp';
    if (@file_put_contents($tmp, $line . "\n") !== false) {
        @rename($tmp, $cfg['progressFile']);
    }
}

function cleanup_progress_temps(array $cfg, int $threads): void {
    if ($cfg['progressFile'] === '') {
        return;
    }
    for ($i = 0; $i < $threads; $i++) {
        @unlink($cfg['progressFile'] . '.w' . $i);
        @unlink($cfg['progressFile'] . '.w' . $i . '.tmp');
    }
    @unlink($cfg['progressFile'] . '.parent.tmp');
}

try {
    if (!function_exists('pcntl_fork')) {
        throw new RuntimeException('pcntl extension missing: needed for forked workers (install php-cli with pcntl enabled)');
    }

    // ---- Configuration (environment only; no CLI arguments) ----
    $host = env_str('MYSQL_HOST');
    $password = getenv('MYSQL_PASSWORD');
    if ($password === false) {
        throw new RuntimeException('missing required environment variable: MYSQL_PASSWORD');
    }
    $cfg = [
        'host' => $host,
        'port' => env_int('MYSQL_PORT'),
        'user' => env_str('MYSQL_USER'),
        'password' => $password,
        'db' => env_str('MYSQL_DB'),
        'workload' => env_int('BENCH_WORKLOAD'),
        'seed' => env_int('BENCH_SEED'),
        'intRows' => env_int_opt('BENCH_INT_ROWS', 1000000),
        'intBatch' => env_int_opt('BENCH_INT_BATCH', 10),
        'blobCount' => env_int_opt('BENCH_BLOB_COUNT', 100),
        'blobSize' => env_int_opt('BENCH_BLOB_SIZE', 1048576),
        'progressFile' => (string) (getenv('BENCH_PROGRESS_FILE') ?: ''),
        'progressSec' => max(1, env_int_opt('BENCH_PROGRESS_SEC', 5)),
    ];

    $W = $cfg['workload'];
    if ($W < 1 || $W > 4) {
        throw new RuntimeException("BENCH_WORKLOAD must be 1..4, got $W");
    }
    if ($cfg['intBatch'] < 1) {
        throw new RuntimeException('BENCH_INT_BATCH must be >= 1');
    }
    $threads = env_int('BENCH_THREADS');
    if ($threads < 1) {
        throw new RuntimeException('BENCH_THREADS must be >= 1');
    }

    $progress = $cfg['progressFile'] !== '';
    $intervalNs = $cfg['progressSec'] * 1000000000;

    if (function_exists('pcntl_signal')) {
        pcntl_signal(SIGPIPE, SIG_IGN); // a dead child must not kill the parent
    }

    // Remove any stale progress artifacts from a previous run before we start.
    if ($progress) {
        @unlink($cfg['progressFile']);
        cleanup_progress_temps($cfg, $threads);
    }

    // ---- Fork workers. Each worker's socketpair is created before its fork;
    // the child end is closed in the parent (and vice versa) right away so the
    // parent never holds more than threads+1 socket fds. ----
    $parentSocks = [];
    $pids = [];
    for ($i = 0; $i < $threads; $i++) {
        $pair = stream_socket_pair(STREAM_PF_UNIX, STREAM_SOCK_STREAM, STREAM_IPPROTO_IP);
        if ($pair === false) {
            throw new RuntimeException('stream_socket_pair failed');
        }
        $pid = pcntl_fork();
        if ($pid === -1) {
            throw new RuntimeException('pcntl_fork failed');
        }
        if ($pid === 0) {
            // Child: keep only its own child-end socket.
            foreach ($parentSocks as $s) {
                fclose($s);
            }
            fclose($pair[0]);
            try {
                worker_main($i, $pair[1], $cfg);
                exit(0);
            } catch (Throwable $e) {
                fwrite(STDERR, "php worker $i error: " . $e->getMessage() . "\n");
                exit(1);
            }
        }
        // Parent.
        fclose($pair[1]);
        $parentSocks[$i] = $pair[0];
        $pids[$i] = $pid;
    }

    // ---- Barrier: wait for every worker's ready byte. ----
    foreach ($parentSocks as $i => $s) {
        $b = fread($s, 1); // blocks; "" (EOF) if the child died
        if ($b !== 'R') {
            fwrite(STDERR, "php worker $i did not become ready\n");
            foreach ($parentSocks as $ps) {
                @fclose($ps); // EOF unblocks the children still waiting to start
            }
            foreach ($pids as $p) {
                pcntl_waitpid($p, $status);
                @unlink(result_file_path($p));
            }
            cleanup_progress_temps($cfg, $threads);
            exit(1);
        }
    }

    // ---- Release: sample t_start, send the start byte to every worker. ----
    $tStartNs = hrtime(true);
    foreach ($parentSocks as $s) {
        @fwrite($s, 'S'); // a failed write means a dead child; waitpid catches it
    }

    // ---- Join all workers, then sample t_end. With progress enabled the parent
    // polls (non-blocking waitpid + sleep) and rewrites the progress file every
    // ~BENCH_PROGRESS_SEC s; otherwise it blocks on each child in turn. ----
    $exitStatus = []; // pid => wait status
    if ($progress) {
        $nextProg = $tStartNs + $intervalNs;
        while (count($exitStatus) < $threads) {
            while (($cpid = pcntl_waitpid(-1, $status, WNOHANG)) > 0) {
                $exitStatus[$cpid] = $status;
            }
            $now = hrtime(true);
            if ($now >= $nextProg) {
                write_parent_progress($cfg, $W, $threads, $tStartNs, $now);
                $nextProg += $intervalNs;
            }
            if (count($exitStatus) < $threads) {
                usleep(10000); // 10 ms between polls
            }
        }
    } else {
        foreach ($pids as $p) {
            pcntl_waitpid($p, $status);
            $exitStatus[$p] = $status;
        }
    }
    $tEndNs = hrtime(true);

    foreach ($parentSocks as $s) {
        @fclose($s);
    }

    // ---- Check for worker failures. ----
    $failed = false;
    foreach ($pids as $i => $p) {
        $st = $exitStatus[$p] ?? -1;
        if ($st === -1 || !pcntl_wifexited($st) || pcntl_wexitstatus($st) !== 0) {
            fwrite(STDERR, "php worker $i exited abnormally\n");
            $failed = true;
        }
    }
    if ($failed) {
        foreach ($pids as $p) {
            @unlink(result_file_path($p));
        }
        // Leave BENCH_PROGRESS_FILE behind (partial result for recovery); drop
        // only the internal per-child temps.
        cleanup_progress_temps($cfg, $threads);
        exit(1);
    }

    // ---- Merge per-worker latency files. ----
    $latencies = [];
    foreach ($pids as $i => $p) {
        $path = result_file_path($p);
        $data = file_get_contents($path);
        if ($data === false) {
            throw new RuntimeException("missing result file for worker $i: $path");
        }
        unlink($path);
        foreach (explode("\n", trim($data)) as $line) {
            if ($line !== '') {
                $latencies[] = (int) $line;
            }
        }
    }

    // ---- Statistics: sort ascending, p(x) = arr[ceil(x/100 * n) - 1]. ----
    sort($latencies);
    $n = count($latencies);
    if ($n < 1) {
        throw new RuntimeException('no latency samples collected');
    }
    $pct = function (int $x) use ($latencies, $n): int {
        return $latencies[(int) ceil($x / 100 * $n) - 1];
    };

    // ---- Derived counts. Per-worker work scales with the worker count. ----
    $perWorker = ($W === 1 || $W === 2) ? $cfg['intRows'] : $cfg['blobCount'];
    $ops = $threads * $perWorker;
    $elapsedS = ($tEndNs - $tStartNs) / 1e9;
    $throughput = $ops / $elapsedS;
    $mbPerS = ($W === 3 || $W === 4)
        ? round($ops * $cfg['blobSize'] / 1e6 / $elapsedS, 1)
        : 0;

    $result = [
        'lang' => 'php',
        'workload' => $W,
        'workload_name' => WL_META[$W][0],
        'unit' => WL_META[$W][1],
        'driver' => 'mysqli',
        'driver_version' => mysqli_get_client_info(), // no connection needed
        'runtime_version' => PHP_VERSION,
        'prepared' => true,
        'client_host' => gethostname(),
        'db_host' => $host,
        'threads' => $threads,
        'ops' => $ops,
        'errors' => 0,
        'elapsed_s' => round($elapsedS, 3),
        'throughput' => round($throughput, 1),
        'mb_per_s' => $mbPerS,
        'p50_us' => $pct(50),
        'p95_us' => $pct(95),
        'p99_us' => $pct(99),
        'max_us' => $latencies[$n - 1],
        'client_cpu_s' => 0, // filled by the harness
        'server_cpu_s' => 0, // filled by the harness
    ];

    echo json_encode($result, JSON_PRESERVE_ZERO_FRACTION), "\n";

    // Success: the progress data is now superseded by stdout.
    if ($progress) {
        cleanup_progress_temps($cfg, $threads);
        @unlink($cfg['progressFile']);
    }
} catch (Throwable $e) {
    fwrite(STDERR, 'error: ' . $e->getMessage() . "\n");
    exit(1);
}
