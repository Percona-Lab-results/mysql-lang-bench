#!/usr/bin/env ruby
# frozen_string_literal: true

# MySQL language benchmark - Ruby implementation (mysql2 gem).
#
# Follows SPEC.md v3 exactly: one of four micro-workloads selected by
# BENCH_WORKLOAD (1..4), run by BENCH_THREADS concurrent workers. Each worker is
# a Ruby Thread owning its own Mysql2::Client (ssl_mode DISABLED, autocommit ON),
# its own prepared statement(s), and its own 31-bit LCG PRNG seeded
# BENCH_SEED + worker_index. Workers connect + prepare, then wait at a
# Queue-based start barrier; main releases it, samples CLOCK_MONOTONIC, joins,
# samples again. Per-operation latency is recorded in truncated integer
# microseconds. One JSON result line is printed on stdout.
#
#   WL1 int-insert : prepared multi-row INSERT (BENCH_INT_BATCH placeholders),
#                    executed BENCH_INT_ROWS / BENCH_INT_BATCH times; one sample
#                    per INSERT; ops = rows.
#   WL2 int-read   : prepared SELECT val FROM bench_int LIMIT n, streamed, summed
#                    in a Ruby Integer; one sample for the whole scan; ops = rows.
#   WL3 blob-insert: generate BENCH_BLOB_SIZE random bytes from the LCG, MD5 them
#                    in app code, prepared INSERT bench_blob(id,data); one sample
#                    per insert; ops = blobs.
#   WL4 blob-read  : prepared SELECT data FROM bench_blob WHERE id = ?, read blob,
#                    MD5 in app code; one sample per read; ops = blobs.
#
# mysql2 is a libmysqlclient C extension that releases the GVL during network
# I/O, so threads overlap on the wire; GVL contention at high thread counts is
# authentic Ruby behaviour (same precedent as Python — do not use processes).

require "json"
require "socket"
require "digest/md5"
require "mysql2"

LCG_MULT = 1_103_515_245
LCG_INC  = 12_345
LCG_MOD  = 2_147_483_648 # 2^31; next() yields a 31-bit value in [0, 2^31 - 1]

WORKLOAD_NAME = {
  1 => "int-insert",
  2 => "int-read",
  3 => "blob-insert",
  4 => "blob-read",
}.freeze
WORKLOAD_UNIT = {
  1 => "rows/s",
  2 => "rows/s",
  3 => "blobs/s",
  4 => "blobs/s",
}.freeze

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
  Integer(val, 10)
rescue ArgumentError
  warn "environment variable #{name} is not an integer: #{val.inspect}"
  exit 1
end

def env_int_opt(name, default)
  val = ENV[name]
  return default if val.nil? || val.empty?

  Integer(val, 10)
rescue ArgumentError
  warn "environment variable #{name} is not an integer: #{val.inspect}"
  exit 1
end

def env_float_opt(name, default)
  val = ENV[name]
  return default if val.nil? || val.empty?

  Float(val)
rescue ArgumentError
  warn "environment variable #{name} is not a number: #{val.inspect}"
  exit 1
end

def monotonic_ns
  Process.clock_gettime(Process::CLOCK_MONOTONIC, :nanosecond)
end

class Worker
  attr_reader :index, :latencies, :sink

  def initialize(index, conn_opts, cfg, counts, ready_queue, start_queue)
    @index = index
    @conn_opts = conn_opts
    @cfg = cfg
    @counts = counts          # shared Array; this worker writes only slot @index
    @ready_queue = ready_queue
    @start_queue = start_queue
    @state = cfg[:seed] + index # own PRNG stream: 31-bit LCG, BENCH_SEED + i
    @latencies = []
    @sink = 0                 # black-box sink so sums/digests are not elided
  end

  def run
    work
  rescue Exception # rubocop:disable Lint/RescueException
    # Never leave main blocked collecting :ready tokens; Thread#join re-raises.
    @ready_queue << :error
    raise
  end

  private

  # 31-bit LCG: state = (state*mult + inc) mod 2^31; return state.
  def next_rand
    @state = (@state * LCG_MULT + LCG_INC) % LCG_MOD
  end

  def new_client
    # ssl_mode DISABLED: libmysqlclient defaults to PREFERRED, which would put
    # this driver on TLS while other languages run plaintext. mysql2 accepts the
    # symbol; fall back to the numeric constant where it is exposed.
    ssl_mode = if defined?(Mysql2::Client::SSL_MODE_DISABLED)
                 Mysql2::Client::SSL_MODE_DISABLED
               else
                 :disabled
               end
    # Autocommit stays ON (libmysqlclient default; mysql2 does not disable it).
    Mysql2::Client.new(@conn_opts.merge(ssl_mode: ssl_mode))
  end

  def work
    client = new_client
    begin
      stmt = prepare(client)         # connect + prepare are outside the timed window
      @ready_queue << :ready
      @start_queue.pop               # blocks until main releases the start barrier

      case @cfg[:workload]
      when 1 then run_int_insert(stmt)
      when 2 then run_int_read(stmt)
      when 3 then run_blob_insert(stmt)
      when 4 then run_blob_read(stmt)
      else
        raise "invalid BENCH_WORKLOAD: #{@cfg[:workload]}"
      end
    ensure
      client.close
    end
  end

  def prepare(client)
    case @cfg[:workload]
    when 1
      placeholders = (["(?)"] * @cfg[:int_batch]).join(",")
      client.prepare("INSERT INTO bench_int (val) VALUES #{placeholders}")
    when 2
      # Stream the scan so a 1M-row result set is not buffered in full (critical
      # at high thread counts). as: :array avoids per-row Hash allocation.
      client.query_options.merge!(as: :array, stream: true, cache_rows: false)
      client.prepare("SELECT val FROM bench_int LIMIT #{@cfg[:int_rows]}")
    when 3
      client.prepare("INSERT INTO bench_blob (id, data) VALUES (?, ?)")
    when 4
      client.query_options.merge!(as: :array)
      client.prepare("SELECT data FROM bench_blob WHERE id = ?")
    end
  end

  # WL1 — integer insert. One prepared multi-row INSERT of BENCH_INT_BATCH rows,
  # executed BENCH_INT_ROWS / BENCH_INT_BATCH times; one sample per INSERT.
  # Each bound value is a fresh next() (in [0, 2^31-1], already a valid signed
  # 32-bit INT, so the "cast to signed 32-bit" is a no-op here). Autocommit ON:
  # each multi-row INSERT commits on its own (SPEC: no explicit transaction).
  def run_int_insert(stmt)
    batch = @cfg[:int_batch]
    iters = @cfg[:int_rows] / batch
    vals = Array.new(batch)
    rows_done = 0
    iters.times do
      b = 0
      while b < batch
        vals[b] = next_rand
        b += 1
      end
      t0 = monotonic_ns
      stmt.execute(*vals)
      @latencies << ((monotonic_ns - t0) / 1000)
      rows_done += batch
      @counts[@index] = rows_done
    end
  end

  # WL2 — integer fetch + sum. Stream every row, sum val into a Ruby Integer
  # (arbitrary precision; a true 64-bit accumulator is sufficient but Ruby's
  # Integer is simpler and cannot overflow), feed to a sink. One sample total.
  def run_int_read(stmt)
    sum = 0
    rows = 0
    t0 = monotonic_ns
    stmt.execute.each do |row|
      sum += row[0]
      rows += 1
      @counts[@index] = rows if (rows & 0xFFFF).zero?
    end
    @latencies << ((monotonic_ns - t0) / 1000)
    @counts[@index] = rows
    @sink ^= (sum & 0xFFFFFFFF)
  end

  # WL3 — blob insert + MD5. Fill ONE BENCH_BLOB_SIZE buffer per worker once with
  # a fast bulk RNG (a per-worker Random, seeded from the worker seed: fast,
  # deterministic, no cross-thread contention). Before each of the BLOB_COUNT
  # blobs, overwrite the first 8 bytes with the blob id so every blob (and its
  # MD5) differs; MD5 still hashes the whole buffer. id = i*BLOB_COUNT + k gives
  # each worker a contiguous id range. Autocommit ON: each INSERT commits itself.
  def run_blob_insert(stmt)
    size = @cfg[:blob_size]
    count = @cfg[:blob_count]
    base = @index * count
    rng = Random.new(@cfg[:seed] + @index)
    buf = rng.bytes(size) # ASCII-8BIT, mutable; mysql2 binds it as a binary blob
    k = 0
    while k < count
      id = base + k
      buf[0, 8] = [id].pack("Q<") # 8 bytes in, 8 bytes out: length unchanged
      @sink ^= Digest::MD5.hexdigest(buf).getbyte(0)
      t0 = monotonic_ns
      stmt.execute(id, buf)
      @latencies << ((monotonic_ns - t0) / 1000)
      k += 1
      @counts[@index] = k
    end
  end

  # WL4 — blob read + MD5. Read each of this worker's own blobs by id, then MD5
  # the content (to the sink). One sample per read (the DB fetch; MD5 is after
  # the sample, mirroring WL3 where MD5 is outside the timed insert).
  def run_blob_read(stmt)
    count = @cfg[:blob_count]
    base = @index * count
    k = 0
    while k < count
      id = base + k
      t0 = monotonic_ns
      data = nil
      stmt.execute(id).each { |row| data = row[0] }
      @latencies << ((monotonic_ns - t0) / 1000)
      raise "bench_blob id #{id} not found" if data.nil?

      @sink ^= Digest::MD5.digest(data).getbyte(0)
      k += 1
      @counts[@index] = k
    end
  end
end

# Atomically overwrite the progress file: write FILE.tmp, then rename over FILE.
def write_progress(path, meta, counts, t_start)
  ops = counts.sum
  elapsed = (monotonic_ns - t_start) / 1e9
  throughput = elapsed.positive? ? (ops / elapsed) : 0.0
  line = JSON.generate(
    "workload" => meta[:workload],
    "workload_name" => meta[:workload_name],
    "unit" => meta[:unit],
    "threads" => meta[:threads],
    "ops" => ops,
    "elapsed_s" => elapsed.round(3),
    "throughput" => throughput.round(1),
    "partial" => true,
  )
  tmp = "#{path}.tmp"
  File.write(tmp, "#{line}\n")
  File.rename(tmp, path)
rescue StandardError => e
  warn "progress write failed: #{e.class}: #{e.message}"
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

  workload = env_int("BENCH_WORKLOAD")
  unless WORKLOAD_NAME.key?(workload)
    warn "BENCH_WORKLOAD must be 1..4, got #{workload}"
    exit 1
  end
  threads = env_int("BENCH_THREADS")
  cfg = {
    workload: workload,
    seed: env_int("BENCH_SEED"),
    int_rows: env_int_opt("BENCH_INT_ROWS", 1_000_000),
    int_batch: env_int_opt("BENCH_INT_BATCH", 10),
    blob_count: env_int_opt("BENCH_BLOB_COUNT", 100),
    blob_size: env_int_opt("BENCH_BLOB_SIZE", 1_048_576),
  }
  blob = [3, 4].include?(workload)

  progress_file = ENV["BENCH_PROGRESS_FILE"]
  progress_file = nil if progress_file.nil? || progress_file.empty?
  progress_sec = env_float_opt("BENCH_PROGRESS_SEC", 5.0)

  # Shared per-worker completed-op counts: worker i writes only counts[i]
  # (no lock); the monitor thread and the final result read the sum.
  counts = Array.new(threads, 0)

  # Start barrier from queues: each worker pushes :ready after connect + prepare,
  # then blocks popping its own start queue; main collects `threads` tokens,
  # samples t_start, and releases everyone at once.
  ready_queue = Queue.new
  start_queues = Array.new(threads) { Queue.new }

  workers = (0...threads).map do |i|
    Worker.new(i, conn_opts, cfg, counts, ready_queue, start_queues[i])
  end
  thread_objs = workers.map { |w| Thread.new { w.run } }

  threads.times { ready_queue.pop } # :ready, or :error from a dying worker
  t_start = monotonic_ns
  start_queues.each { |q| q << :go }

  # Progress monitor: every progress_sec seconds it sums counts and atomically
  # rewrites the progress file. A ConditionVariable lets main wake it to stop.
  meta = {
    workload: workload,
    workload_name: WORKLOAD_NAME[workload],
    unit: WORKLOAD_UNIT[workload],
    threads: threads,
  }
  stop_mutex = Mutex.new
  stop_cond = ConditionVariable.new
  stopped = false
  monitor = nil
  if progress_file
    monitor = Thread.new do
      loop do
        should_stop = stop_mutex.synchronize do
          stop_cond.wait(stop_mutex, progress_sec) unless stopped
          stopped
        end
        break if should_stop

        write_progress(progress_file, meta, counts, t_start)
      end
    end
  end

  begin
    thread_objs.each(&:join) # re-raises the first worker exception
  rescue Exception => e # rubocop:disable Lint/RescueException
    warn "worker error: #{e.class}: #{e.message}"
    (e.backtrace || []).first(5).each { |line| warn "  #{line}" }
    exit 1 # leave any .progress file behind to mark the interrupted run
  end
  t_end = monotonic_ns

  if monitor
    stop_mutex.synchronize do
      stopped = true
      stop_cond.signal
    end
    monitor.join
  end

  elapsed_s = (t_end - t_start) / 1e9

  latencies = workers.flat_map(&:latencies)
  latencies.sort!
  n = latencies.length
  # p(x) = arr[ceil(x/100 * n) - 1], 0-based (integer ceil).
  pctile = ->(x) { n.zero? ? 0 : latencies[(x * n + 99) / 100 - 1] }

  ops = counts.sum
  throughput = elapsed_s.positive? ? (ops / elapsed_s) : 0.0
  mb_per_s = if blob && elapsed_s.positive?
               (ops * cfg[:blob_size] / 1e6 / elapsed_s).round(1)
             else
               0
             end

  # Reference the sink so the summation / MD5 work cannot be considered dead.
  sink = workers.reduce(0) { |acc, w| acc ^ w.sink }

  result = {
    "lang" => "ruby",
    "workload" => workload,
    "workload_name" => WORKLOAD_NAME[workload],
    "unit" => WORKLOAD_UNIT[workload],
    "driver" => "mysql2",
    "driver_version" => Mysql2::VERSION,
    "runtime_version" => RUBY_VERSION,
    "prepared" => true,
    "client_host" => Socket.gethostname,
    "db_host" => conn_opts[:host],
    "threads" => threads,
    "ops" => ops,
    "errors" => 0,
    "elapsed_s" => elapsed_s.round(3),
    "throughput" => throughput.round(1),
    "mb_per_s" => mb_per_s,
    "p50_us" => pctile.call(50),
    "p95_us" => pctile.call(95),
    "p99_us" => pctile.call(99),
    "max_us" => n.zero? ? 0 : latencies[n - 1],
    "client_cpu_s" => 0,
    "server_cpu_s" => 0,
  }
  warn "sink=#{sink}" if ENV["BENCH_DEBUG_SINK"]
  puts JSON.generate(result)

  # Success: stdout now supersedes the progress file; remove it.
  File.delete(progress_file) if progress_file && File.exist?(progress_file)
end

main if __FILE__ == $PROGRAM_NAME
