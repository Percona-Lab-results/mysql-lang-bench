-- v3 micro-workload tables. No secondary indexes: maximize insert speed.
DROP TABLE IF EXISTS bench_int;
DROP TABLE IF EXISTS bench_blob;

CREATE TABLE bench_int (
  val INT NOT NULL
) ENGINE=InnoDB;

CREATE TABLE bench_blob (
  id   BIGINT NOT NULL PRIMARY KEY,
  data LONGBLOB NOT NULL
) ENGINE=InnoDB;
