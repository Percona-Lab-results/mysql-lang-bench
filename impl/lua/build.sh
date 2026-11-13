#!/usr/bin/env bash
# Build step for the Lua implementation: nothing to compile, just verify that
# LuaJIT and the shared libraries bench.lua loads (libmysqlclient for the DB
# driver, libcrypto for MD5) are available via the same ffi.load fallback
# chains the program uses.
set -euo pipefail

if ! command -v luajit >/dev/null 2>&1; then
    echo "luajit not found: install LuaJIT (e.g. apt install luajit)" >&2
    exit 1
fi

if ! luajit -e '
local ffi = require("ffi")
local names = { "mysqlclient", "libmysqlclient.so.24",
                "libmysqlclient.so.21", "libmysqlclient.so" }
for _, name in ipairs(names) do
    if pcall(ffi.load, name) then os.exit(0) end
end
os.exit(1)' >/dev/null 2>&1; then
    echo "libmysqlclient shared library not found: install the MySQL client library (e.g. apt install libmysqlclient-dev or libmysqlclient21)" >&2
    exit 1
fi

if ! luajit -e '
local ffi = require("ffi")
ffi.cdef("unsigned char *MD5(const unsigned char *d, size_t n, unsigned char *md);")
local names = { "crypto", "libcrypto.so.3", "libcrypto.so.1.1", "libcrypto.so" }
for _, name in ipairs(names) do
    local ok, lib = pcall(ffi.load, name)
    if ok and pcall(function () return lib.MD5 end) then os.exit(0) end
end
os.exit(1)' >/dev/null 2>&1; then
    echo "libcrypto (OpenSSL) with the MD5 symbol not found: install OpenSSL (e.g. apt install libssl-dev or libssl3)" >&2
    exit 1
fi

exit 0
