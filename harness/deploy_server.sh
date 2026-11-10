#!/usr/bin/env bash
# Usage: deploy_server.sh <host>
# Copies the Percona Server tarball + my.cnf to the host, then unpacks,
# initializes and starts it there (idempotent).
source "$(dirname "$0")/lib.sh"
HOST=$1
PS_NAME="Percona-Server-8.4.11-11-Linux.x86_64.glibc2.35-minimal"
LOCAL_TARBALL="$HOME/servers/$PS_NAME.tar.gz"
[ -f "$LOCAL_TARBALL" ] || { echo "local tarball missing: $LOCAL_TARBALL" >&2; exit 1; }

rssh "$HOST" "mkdir -p servers"
# shellcheck disable=SC2086
rsync -a --partial -e "ssh $SSH_OPTS" "$LOCAL_TARBALL" "$SSH_USER@$HOST:servers/"
# shellcheck disable=SC2086
rsync -a -e "ssh $SSH_OPTS" "$ROOT/config/server.my.cnf" "$SSH_USER@$HOST:servers/my.cnf"
rssh "$HOST" 'bash -s' < "$ROOT/harness/remote_server_setup.sh"
