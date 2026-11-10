<?php
// MySQL language benchmark - PHP implementation (v2: sysbench oltp_read_write-style).
// mysqli + server-side prepared statements; BENCH_THREADS workers as forked
// processes (pcntl_fork) since PHP has no threads. See SPEC.md.
//
// Parent/child protocol:
//   - a per-worker UNIX stream socketpair is created before each fork;
//   - the child connects to MySQL (connections are created only AFTER fork),
//     prepares statements, runs warmup txns, writes "R" (ready) to its socket
//     and blocks reading one byte (the start barrier);
//   - the parent waits for all "R" bytes, samples t_start, writes "S" to every
//     child, waits for all children, samples t_end;
//   - each child writes its latencies (one integer microsecond value per line)
//     plus a final "retries=<n>" line to $TMPDIR/bench-php-<pid>.result;
//     the parent merges those files and prints the single JSON result line.

mysqli_report(MYSQLI_REPORT_ERROR | MYSQLI_REPORT_STRICT);

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

// ---- PRNG: 31-bit LCG. One independent stream per worker process: the child
// seeds this with BENCH_SEED + worker_index after the fork. ----
$lcg_state = 0;

function lcg_next(): int {
    global $lcg_state;
    // PHP ints are 64-bit: state < 2^31, so state * 1103515245 + 12345 < 2^63.
    $lcg_state = ($lcg_state * 1103515245 + 12345) % 2147483648;
    return $lcg_state;
}

function lcg_rand(int $n): int {
    return lcg_next() % $n;
}

function result_file_path(int $pid): string {
    return sys_get_temp_dir() . '/bench-php-' . $pid . '.result';
}

// ---- Worker body: runs in the forked child only. ----
function worker_main(int $workerIdx, $sock, array $cfg): void {
    global $lcg_state;
    $lcg_state = $cfg['seed'] + $workerIdx;

    $N = $cfg['tableRows'];
    $R = $cfg['rangeSize'];
    $cVal = str_repeat('abcdefghij', 12);   // 120 chars
    $padVal = str_repeat('0123456789', 6);  // 60 chars

    // Connection (child only, after fork): TCP, autocommit ON (default),
    // explicit BEGIN/COMMIT as plain queries.
    $conn = new mysqli($cfg['host'], $cfg['user'], $cfg['password'], $cfg['db'], $cfg['port']);

    // The 18 inner statements of the transaction, server-side prepared once per
    // worker (9 distinct statement handles; the point select runs 10x per txn).
    $pointStmt    = $conn->prepare('SELECT c FROM sbtest WHERE id = ?');
    $rangeStmt    = $conn->prepare('SELECT c FROM sbtest WHERE id BETWEEN ? AND ?');
    $sumStmt      = $conn->prepare('SELECT SUM(k) FROM sbtest WHERE id BETWEEN ? AND ?');
    $orderStmt    = $conn->prepare('SELECT c FROM sbtest WHERE id BETWEEN ? AND ? ORDER BY c');
    $distinctStmt = $conn->prepare('SELECT DISTINCT c FROM sbtest WHERE id BETWEEN ? AND ? ORDER BY c');
    $idxUpdStmt   = $conn->prepare('UPDATE sbtest SET k = k + 1 WHERE id = ?');
    $cUpdStmt     = $conn->prepare('UPDATE sbtest SET c = ? WHERE id = ?');
    $delStmt      = $conn->prepare('DELETE FROM sbtest WHERE id = ?');
    $insStmt      = $conn->prepare('INSERT INTO sbtest (id, k, c, pad) VALUES (?, ?, ?, ?)');

    // bind_param binds by reference: bind once, mutate the variables per draw.
    $pointId = 0; $lo = 0; $hi = 0; $updId = 0; $cUpdId = 0; $delId = 0; $insK = 0;
    $pointStmt->bind_param('i', $pointId);
    $rangeStmt->bind_param('ii', $lo, $hi);
    $sumStmt->bind_param('ii', $lo, $hi);
    $orderStmt->bind_param('ii', $lo, $hi);
    $distinctStmt->bind_param('ii', $lo, $hi);
    $idxUpdStmt->bind_param('i', $updId);
    $cUpdStmt->bind_param('si', $cVal, $cUpdId);
    $delStmt->bind_param('i', $delId);
    $insStmt->bind_param('iiss', $delId, $insK, $cVal, $padVal);

    // Execute a SELECT and fetch all returned rows.
    $fetchAll = function (mysqli_stmt $stmt): void {
        $stmt->execute();
        $res = $stmt->get_result();
        while ($res->fetch_row()) {
            // row arrays are non-empty and therefore truthy; null ends the loop
        }
        $res->free();
    };

    // One attempt at the sysbench oltp_read_write transaction. Values are
    // drawn immediately before executing each statement (draw order =
    // execution order); a retry redraws everything (the PRNG stream continues).
    $runTxnOnce = function () use (
        $conn, $fetchAll,
        $pointStmt, $rangeStmt, $sumStmt, $orderStmt, $distinctStmt,
        $idxUpdStmt, $cUpdStmt, $delStmt, $insStmt,
        &$pointId, &$lo, &$hi, &$updId, &$cUpdId, &$delId, &$insK,
        $N, $R
    ): void {
        $conn->query('BEGIN');
        for ($s = 0; $s < 10; $s++) {           // 10 x point select
            $pointId = 1 + lcg_rand($N);
            $fetchAll($pointStmt);
        }
        $lo = 1 + lcg_rand($N); $hi = $lo + $R - 1;  // simple range
        $fetchAll($rangeStmt);
        $lo = 1 + lcg_rand($N); $hi = $lo + $R - 1;  // sum range
        $fetchAll($sumStmt);
        $lo = 1 + lcg_rand($N); $hi = $lo + $R - 1;  // order range
        $fetchAll($orderStmt);
        $lo = 1 + lcg_rand($N); $hi = $lo + $R - 1;  // distinct range
        $fetchAll($distinctStmt);
        $updId = 1 + lcg_rand($N);                   // index update
        $idxUpdStmt->execute();
        $cUpdId = 1 + lcg_rand($N);                  // non-index update
        $cUpdStmt->execute();
        $delId = 1 + lcg_rand($N);                   // delete + insert (same id)
        $delStmt->execute();
        $insK = lcg_rand($N);
        $insStmt->execute();
        $conn->query('COMMIT');
    };

    // Retry wrapper: deadlock (1213), lock wait timeout (1205) and duplicate
    // key (1062) anywhere in the transaction (COMMIT included) roll back,
    // count a retry and re-run the whole transaction with fresh draws.
    $retries = 0;
    $runTxn = function () use ($conn, $runTxnOnce, &$retries): void {
        while (true) {
            try {
                $runTxnOnce();
                return;
            } catch (mysqli_sql_exception $e) {
                if (!in_array($e->getCode(), [1213, 1205, 1062], true)) {
                    throw $e;
                }
                try {
                    $conn->query('ROLLBACK');
                } catch (Throwable $ignored) {
                    // rollback errors are ignored per SPEC
                }
                $retries++;
            }
        }
    };

    // Warmup (untimed, before the barrier).
    for ($w = 0; $w < $cfg['warmupTxns']; $w++) {
        $runTxn();
    }
    $retries = 0; // report retries from the timed window only

    // Barrier: signal ready, then block until the parent sends the start byte.
    if (fwrite($sock, 'R') !== 1) {
        throw new RuntimeException('failed to write ready byte to parent');
    }
    $start = fread($sock, 1); // blocking read: this is the barrier
    if ($start !== 'S') {
        throw new RuntimeException('did not receive start signal from parent');
    }

    // Timed run: per-txn latency in integer microseconds (truncated), from
    // just before the first BEGIN to after the successful COMMIT, retries
    // included. Latency array is pre-allocated.
    $txns = $cfg['txnsPerThread'];
    $latencies = array_fill(0, $txns, 0);
    for ($t = 0; $t < $txns; $t++) {
        $t0 = hrtime(true);
        $runTxn();
        $latencies[$t] = intdiv(hrtime(true) - $t0, 1000);
    }

    $conn->close();

    $path = result_file_path(getmypid());
    $out = implode("\n", $latencies) . "\nretries=" . $retries . "\n";
    if (file_put_contents($path, $out) === false) {
        throw new RuntimeException("failed to write result file: $path");
    }
}

try {
    if (!function_exists('pcntl_fork')) {
        throw new RuntimeException('pcntl extension missing: install it (e.g. apt install php-cli with pcntl enabled)');
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
        'tableRows' => env_int('BENCH_TABLE_ROWS'),
        'rangeSize' => env_int('BENCH_RANGE_SIZE'),
        'warmupTxns' => env_int('BENCH_WARMUP_TXNS'),
        'txnsPerThread' => env_int('BENCH_TXNS_PER_THREAD'),
        'seed' => env_int('BENCH_SEED'),
    ];
    $threads = env_int('BENCH_THREADS');
    if ($threads < 1) {
        throw new RuntimeException('BENCH_THREADS must be >= 1');
    }

    if (function_exists('pcntl_signal')) {
        pcntl_signal(SIGPIPE, SIG_IGN); // a dead child must not kill the parent
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
            exit(1);
        }
    }

    // ---- Release: sample t_start, send the start byte to every worker. ----
    $tStartNs = hrtime(true);
    foreach ($parentSocks as $s) {
        @fwrite($s, 'S'); // a failed write means a dead child; waitpid catches it
    }

    // ---- Join all workers, then sample t_end. ----
    $failed = false;
    foreach ($pids as $i => $p) {
        pcntl_waitpid($p, $status);
        if (!pcntl_wifexited($status) || pcntl_wexitstatus($status) !== 0) {
            fwrite(STDERR, "php worker $i exited abnormally\n");
            $failed = true;
        }
    }
    $tEndNs = hrtime(true);

    foreach ($parentSocks as $s) {
        @fclose($s);
    }
    if ($failed) {
        foreach ($pids as $p) {
            @unlink(result_file_path($p));
        }
        exit(1);
    }

    // ---- Merge per-worker result files. ----
    $latencies = [];
    $retries = 0;
    foreach ($pids as $i => $p) {
        $path = result_file_path($p);
        $data = file_get_contents($path);
        if ($data === false) {
            throw new RuntimeException("missing result file for worker $i: $path");
        }
        unlink($path);
        $lines = explode("\n", trim($data));
        $last = array_pop($lines);
        if (strncmp($last, 'retries=', 8) !== 0) {
            throw new RuntimeException("malformed result file for worker $i: $path");
        }
        $retries += (int) substr($last, 8);
        if (count($lines) !== $cfg['txnsPerThread']) {
            throw new RuntimeException(
                "worker $i reported " . count($lines) . ' latencies, expected ' . $cfg['txnsPerThread']
            );
        }
        foreach ($lines as $line) {
            $latencies[] = (int) $line;
        }
    }

    // ---- Statistics: sort ascending, p(x) = arr[ceil(x/100 * n) - 1] ----
    sort($latencies);
    $n = count($latencies);
    $pct = function (int $x) use ($latencies, $n): int {
        return $latencies[(int) ceil($x / 100 * $n) - 1];
    };

    $txns = $threads * $cfg['txnsPerThread'];
    $elapsedS = ($tEndNs - $tStartNs) / 1e9;
    $tps = $txns / $elapsedS;

    $result = [
        'lang' => 'php',
        'driver' => 'mysqli',
        'driver_version' => mysqli_get_client_info(), // no connection needed
        'runtime_version' => PHP_VERSION,
        'prepared' => true,
        'client_host' => gethostname(),
        'db_host' => $host,
        'threads' => $threads,
        'txns' => $txns,
        'retries' => $retries,
        'errors' => 0,
        'elapsed_s' => round($elapsedS, 3),
        'tps' => round($tps, 1),
        'qps' => round($tps * 20, 1), // 18 statements + BEGIN + COMMIT
        'p50_us' => $pct(50),
        'p95_us' => $pct(95),
        'p99_us' => $pct(99),
        'max_us' => $latencies[$n - 1],
    ];

    echo json_encode($result, JSON_PRESERVE_ZERO_FRACTION), "\n";
} catch (Throwable $e) {
    fwrite(STDERR, 'error: ' . $e->getMessage() . "\n");
    exit(1);
}
