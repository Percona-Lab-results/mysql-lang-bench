#!/usr/bin/env bash
# Build the Swift benchmark (release). Idempotent. Works when invoked from the repo root.
set -euo pipefail

cd "$(dirname "$0")"

# The Swift toolchain is not on PATH in non-interactive ssh; bench hosts keep it in ~/swift.
if command -v swift >/dev/null 2>&1; then
    SWIFT=swift
elif [[ -x "$HOME/swift/usr/bin/swift" ]]; then
    SWIFT="$HOME/swift/usr/bin/swift"
else
    echo "error: swift not found — install the Swift toolchain to ~/swift (expected $HOME/swift/usr/bin/swift)" >&2
    exit 1
fi

# Headers/lib come from Ubuntu's libmysqlclient-dev (no mysqlclient.pc, so we
# only sanity-check mysql_config; the CMySQL module map uses the absolute header
# path /usr/include/mysql/mysql.h).
if ! command -v mysql_config >/dev/null 2>&1; then
    echo "error: mysql_config not found — install libmysqlclient-dev (Debian/Ubuntu) or mysql-devel (RHEL/Fedora)" >&2
    exit 1
fi

# MD5 comes from OpenSSL's libcrypto via the CCrypto module map, which names
# <openssl/md5.h> by absolute path and links -lcrypto. Both ship in libssl-dev.
if [[ ! -f /usr/include/openssl/md5.h ]]; then
    echo "error: /usr/include/openssl/md5.h not found — install libssl-dev (Debian/Ubuntu) or openssl-devel (RHEL/Fedora)" >&2
    exit 1
fi

"$SWIFT" build -c release
