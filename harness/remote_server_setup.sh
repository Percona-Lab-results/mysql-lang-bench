#!/usr/bin/env bash
# Runs ON a benchmark host: unpack Percona Server tarball, init ~/servers/data,
# start mysqld with ~/servers/my.cnf, create the bench db/user. Idempotent.
set -euo pipefail
SERVERS="$HOME/servers"
PS_NAME="Percona-Server-8.4.11-11-Linux.x86_64.glibc2.35-minimal"
TARBALL="$SERVERS/$PS_NAME.tar.gz"
PS="$SERVERS/$PS_NAME"
DATA="$SERVERS/data"
CNF="$SERVERS/my.cnf"
SOCK=/tmp/mysql-alloc-test.sock

echo "== resources: $(free -g | awk '/^Mem:/{print $2"G RAM"}'), $(df -h --output=avail "$SERVERS" | tail -1 | tr -d ' ') free on $SERVERS"

[ -f "$TARBALL" ] || { echo "tarball missing: $TARBALL" >&2; exit 1; }
[ -f "$CNF" ] || { echo "config missing: $CNF" >&2; exit 1; }
[ -d "$PS" ] || tar xzf "$TARBALL" -C "$SERVERS"

if "$PS/bin/mysqladmin" --no-defaults --socket="$SOCK" --connect-timeout=3 ping >/dev/null 2>&1; then
    echo "mysqld already running on $SOCK"
else
    # A datadir from a previous (different) server setup cannot be reused by
    # 8.4.11. Move it aside (never delete); our own datadir carries a marker.
    if [ -d "$DATA" ] && [ ! -f "$DATA/.mysql-lang-bench" ] && [ -n "$(ls -A "$DATA" 2>/dev/null)" ]; then
        BAK="$SERVERS/data-bak-$(date +%Y%m%d)"
        n=0; while [ -e "$BAK" ]; do n=$((n+1)); BAK="$SERVERS/data-bak-$(date +%Y%m%d)-$n"; done
        echo "moving foreign data dir aside: $DATA -> $BAK"
        mv "$DATA" "$BAK"
    fi
    if [ ! -d "$DATA/mysql" ]; then
        mkdir -p "$DATA"
        echo "initializing data directory $DATA"
        "$PS/bin/mysqld" --defaults-file="$CNF" --initialize-insecure
        touch "$DATA/.mysql-lang-bench"
    fi
    echo "starting mysqld"
    setsid "$PS/bin/mysqld" --defaults-file="$CNF" >/dev/null 2>&1 < /dev/null &
    up=0
    for _ in $(seq 1 90); do
        if "$PS/bin/mysqladmin" --no-defaults --socket="$SOCK" ping >/dev/null 2>&1; then
            up=1; break
        fi
        sleep 2
    done
    [ "$up" = 1 ] || { echo "mysqld did not come up; tail of error log:" >&2; tail -30 "$DATA/mysql-error.log" >&2; exit 1; }
fi

"$PS/bin/mysql" --no-defaults -uroot --socket="$SOCK" -e "
CREATE DATABASE IF NOT EXISTS bench;
CREATE USER IF NOT EXISTS 'bench'@'localhost' IDENTIFIED BY 'bench';
CREATE USER IF NOT EXISTS 'bench'@'127.0.0.1' IDENTIFIED BY 'bench';
GRANT ALL PRIVILEGES ON bench.* TO 'bench'@'localhost';
GRANT ALL PRIVILEGES ON bench.* TO 'bench'@'127.0.0.1';"

"$PS/bin/mysql" --no-defaults --protocol=TCP -h127.0.0.1 -P3306 -ubench -pbench \
    -e "SELECT @@version, @@version_comment" bench
echo "SERVER-OK $(hostname)"
