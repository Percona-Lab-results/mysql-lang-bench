#!/usr/bin/env bash
# Usage: deploy.sh <host>   — rsync the repo to the remote host.
source "$(dirname "$0")/lib.sh"
HOST=$1

[ -f "$ROOT/config/mysql.env" ] || {
    echo "config/mysql.env missing — copy config/mysql.env.example and fill it in" >&2
    exit 1
}

# shellcheck disable=SC2086
rsync -az --delete \
    --exclude 'results/' --exclude '.git/' \
    --exclude 'impl/rust/target/' --exclude 'impl/java/lib/' \
    --exclude 'impl/python/venv/' --exclude '*.o' --exclude 'impl/*/bench' \
    --exclude 'impl/java/*.class' --exclude 'impl/javascript/node_modules/' \
    --exclude 'impl/typescript/node_modules/' --exclude 'impl/typescript/dist/' \
    --exclude 'impl/csharp/bin/' --exclude 'impl/csharp/obj/' \
    --exclude 'impl/swift/.build/' \
    -e "ssh $SSH_OPTS" "$ROOT/" "$SSH_USER@$HOST:$REMOTE_DIR/"
echo "deployed to $HOST:$REMOTE_DIR"
