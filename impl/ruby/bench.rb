#!/usr/bin/env ruby
# frozen_string_literal: true

# MySQL language benchmark - Ruby implementation (mysql2 gem).
#
# Follows SPEC.md v2 exactly: multithreaded sysbench-oltp_read_write-style
# transactional workload. One Thread per worker, each with its own
# Mysql2::Client (autocommit ON, explicit BEGIN/COMMIT as plain queries), its
# own server-side prepared statements, its own 31-bit LCG PRNG seeded
# BENCH_SEED + worker_index, barrier-based timing, retry on MySQL errors
# 1213/1205/1062 with full value redraw, per-transaction latency in integer
# microseconds, one JSON result line on stdout.
#
# mysql2 is a libmysqlclient C extension that releases the GVL during network
# I/O, so threads overlap on the wire; GVL contention at high thread counts is
# authentic Ruby behaviour (same precedent as the Python implementation — do
# not use processes).

require "json"
require "socket"
require "mysql2"

C_VAL = "abcdefghij" * 12   # 120 chars
PAD_VAL = "0123456789" * 6  # 60 chars

RETRYABLE = [1213, 1205, 1062].freeze # deadlock, lock wait timeout, dup key

SQL_POINT         = "SELECT c FROM sbtest WHERE id = ?"
SQL_RANGE         = "SELECT c FROM sbtest WHERE id BETWEEN ? AND ?"
SQL_SUM           = "SELECT SUM(k) FROM sbtest WHERE id BETWEEN ? AND ?"
SQL_ORDER         = "SELECT c FROM sbtest WHERE id BETWEEN ? AND ? ORDER BY c"
SQL_DISTINCT      = "SELECT DISTINCT c FROM sbtest WHERE id BETWEEN ? AND ? ORDER BY c"
SQL_IDX_UPDATE    = "UPDATE sbtest SET k = k + 1 WHERE id = ?"
SQL_NONIDX_UPDATE = "UPDATE sbtest SET c = ? WHERE id = ?"
SQL_DELETE        = "DELETE FROM sbtest WHERE id = ?"
SQL_INSERT        = "INSERT INTO sbtest (id, k, c, pad) VALUES (?, ?, ?, ?)"

def env_str(name)
  val = ENV[name]
  if val.nil?
    warn "missing required environment variable: #{name}"
    exit 1
  end
  val
end

def env_int(name)
  val = env_str(name)
  begin
    Integer(val, 10)
  rescue ArgumentError
    warn "environment variable #{name} is not an integer: #{val.inspect}"
    exit 1
  end
end

def env_int_opt(name, default)
  val = ENV[name]
  return default if val.nil? || val.empty?

  begin
    Integer(val, 10)
  rescue ArgumentError
    warn "environment variable #{name} is not an integer: #{val.inspect}"
    exit 1
  end
end

def monotonic_ns
  Process.clock_gettime(Process::CLOCK_MONOTONIC, :nanosecond)
end

class Worker
  attr_reader :index, :latencies, :retries

  def initialize(index, conn_opts, table_rows, range_size, warmup_txns, txns,
                 duration_s, seed, ready_queue, start_queue)
    @index = index
    @conn_opts = conn_opts
    @table_rows = table_rows
    @range_size = range_size
    @warmup_txns = warmup_txns
    @txns = txns
    @duration_ns = duration_s * 1_000_000_000
    @state = seed + index # own PRNG stream: 31-bit LCG, seeded BENCH_SEED + i
    @ready_queue = ready_queue
    @start_queue = start_queue
    # Events mode: pre-allocated (integer microseconds). Duration mode: the
    # transaction count is unknown up front, so the array grows dynamically.
    @latencies = duration_s.positive? ? [] : Array.new(txns, 0)
    @retries = 0
  end

  def run
    work
  rescue Exception # rubocop:disable Lint/RescueException
    # Never leave main blocked on the ready queue; Thread#join re-raises.
    @ready_queue << :error
    raise
  end

  private

  def rand_n(n)
    @state = (@state * 1103515245 + 12345) % 2_147_483_648
    @state % n
  end

  def work
    # ssl_mode DISABLED: libmysqlclient defaults to PREFERRED, which would put
    # this driver on TLS while other languages run plaintext. mysql2 accepts
    # the symbol form; fall back to the numeric constant if available.
    ssl_mode = if defined?(Mysql2::Client::SSL_MODE_DISABLED)
                 Mysql2::Client::SSL_MODE_DISABLED
               else
                 :disabled
               end
    client = Mysql2::Client.new(@conn_opts.merge(ssl_mode: ssl_mode))
    # Autocommit stays ON (libmysqlclient default; mysql2 does not disable it);
    # transactions are explicit BEGIN/COMMIT plain queries per SPEC.md.
    begin
      @stmt_point         = client.prepare(SQL_POINT)
      @stmt_range         = client.prepare(SQL_RANGE)
      @stmt_sum           = client.prepare(SQL_SUM)
      @stmt_order         = client.prepare(SQL_ORDER)
      @stmt_distinct      = client.prepare(SQL_DISTINCT)
      @stmt_idx_update    = client.prepare(SQL_IDX_UPDATE)
      @stmt_nonidx_update = client.prepare(SQL_NONIDX_UPDATE)
      @stmt_delete        = client.prepare(SQL_DELETE)
      @stmt_insert        = client.prepare(SQL_INSERT)

      @warmup_txns.times { txn(client) }

      @retries = 0 # report retries from the timed window only
      @ready_queue << :ready
      @start_queue.pop # blocks until main releases the start barrier

      if @duration_ns.positive?
        # Duration mode: deadline checked before each new transaction; a
        # transaction in flight at the deadline (retries included) completes.
        start = monotonic_ns
        deadline = start + @duration_ns
        while monotonic_ns < deadline
          t0 = monotonic_ns
          txn(client)
          @latencies << (monotonic_ns - t0) / 1000 # truncated integer us
        end
      else
        @txns.times do |j|
          t0 = monotonic_ns
          txn(client)
          @latencies[j] = (monotonic_ns - t0) / 1000 # truncated integer us
        end
      end
    ensure
      client.close
    end
  end

  # Re-run (with freshly drawn values) until it commits.
  def txn(client)
    attempt(client)
  rescue Mysql2::Error => e
    raise unless RETRYABLE.include?(e.error_number)

    begin
      client.query("ROLLBACK")
    rescue Mysql2::Error
      # ignore rollback errors
    end
    @retries += 1
    retry
  end

  # One transaction attempt; values drawn immediately before each statement
  # (draw order = execution order). Every SELECT consumes all rows.
  def attempt(client)
    n = @table_rows
    r = @range_size

    client.query("BEGIN")
    10.times do
      consume(@stmt_point.execute(1 + rand_n(n)))
    end
    lo = 1 + rand_n(n)
    consume(@stmt_range.execute(lo, lo + r - 1))
    lo = 1 + rand_n(n)
    consume(@stmt_sum.execute(lo, lo + r - 1)) # one row, SUM possibly NULL
    lo = 1 + rand_n(n)
    consume(@stmt_order.execute(lo, lo + r - 1))
    lo = 1 + rand_n(n)
    consume(@stmt_distinct.execute(lo, lo + r - 1))
    @stmt_idx_update.execute(1 + rand_n(n))
    @stmt_nonidx_update.execute(C_VAL, 1 + rand_n(n))
    row_id = 1 + rand_n(n)
    k = rand_n(n)
    @stmt_delete.execute(row_id)
    @stmt_insert.execute(row_id, k, C_VAL, PAD_VAL)
    client.query("COMMIT")
  end

  def consume(result)
    result&.each { |_row| } # fetch all returned rows
  end
end

def main
  Thread.report_on_exception = false # main reports worker errors via #join

  conn_opts = {
    host: env_str("MYSQL_HOST"),
    port: env_int("MYSQL_PORT"),
    username: env_str("MYSQL_USER"),
    password: env_str("MYSQL_PASSWORD"),
    database: env_str("MYSQL_DB"),
  }

  table_rows = env_int("BENCH_TABLE_ROWS")
  threads = env_int("BENCH_THREADS")
  warmup_txns = env_int("BENCH_WARMUP_TXNS")
  txns_per_thread = env_int("BENCH_TXNS_PER_THREAD")
  range_size = env_int("BENCH_RANGE_SIZE")
  seed = env_int("BENCH_SEED")
  duration_s = env_int_opt("BENCH_DURATION_S", 0) # 0 = events mode

  # Start barrier built from queues: each worker pushes to the shared ready
  # queue after connect + prepare + warmup, then blocks popping its own start
  # queue; main collects `threads` ready tokens, samples t_start and releases
  # everyone at once.
  ready_queue = Queue.new
  start_queues = Array.new(threads) { Queue.new }

  workers = (0...threads).map do |i|
    Worker.new(i, conn_opts, table_rows, range_size, warmup_txns,
               txns_per_thread, duration_s, seed, ready_queue, start_queues[i])
  end
  thread_objs = workers.map { |w| Thread.new { w.run } }

  threads.times { ready_queue.pop } # :ready, or :error from a dying worker
  t_start = monotonic_ns
  start_queues.each { |q| q << :go }

  begin
    thread_objs.each(&:join) # re-raises the first worker exception
  rescue Exception => e # rubocop:disable Lint/RescueException
    warn "worker error: #{e.class}: #{e.message}"
    (e.backtrace || []).first(5).each { |line| warn "  #{line}" }
    exit 1
  end
  t_end = monotonic_ns

  elapsed_s = (t_end - t_start) / 1e9

  latencies = workers.flat_map(&:latencies)
  latencies.sort!
  n = latencies.length
  # p(x) = arr[ceil(x/100 * n) - 1], 0-based (integer ceil)
  pctile = ->(x) { latencies[(x * n + 99) / 100 - 1] }

  driver_version = Mysql2::VERSION.to_s
  begin
    libver = Mysql2::Client.info[:version]
    driver_version += " (libmysqlclient #{libver})" if libver
  rescue StandardError
    # client library info unavailable; report the gem version alone
  end

  # Duration mode: total committed timed transactions across workers (one
  # latency sample per committed transaction). Events mode: fixed count.
  txns = duration_s.positive? ? n : threads * txns_per_thread
  tps = txns / elapsed_s
  result = {
    "lang" => "ruby",
    "driver" => "mysql2",
    "driver_version" => driver_version,
    "runtime_version" => RUBY_VERSION,
    "prepared" => true,
    "client_host" => Socket.gethostname,
    "db_host" => conn_opts[:host],
    "threads" => threads,
    "txns" => txns,
    "retries" => workers.sum(&:retries),
    "errors" => 0,
    "elapsed_s" => elapsed_s.round(3),
    "tps" => tps.round(1),
    "qps" => (tps * 20).round(1),
    "p50_us" => pctile.call(50),
    "p95_us" => pctile.call(95),
    "p99_us" => pctile.call(99),
    "max_us" => latencies[n - 1],
  }
  puts JSON.generate(result)
end

main if __FILE__ == $PROGRAM_NAME
