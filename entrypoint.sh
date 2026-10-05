#!/bin/sh
# When started as root (the default), repair /data ownership — volumes created
# by pre-1.2 root containers are root-owned and unreadable for the app user —
# then drop privileges to pnpb (uid 10001). When started with --user, run as-is.
set -e

PORT="${PNPB_PORT:-8060}"

if [ "$(id -u)" = "0" ]; then
    # Only when something is actually foreign-owned: a recursive chown on every
    # start rewrites every inode of the volume (slow, touches ctime/backups).
    if [ -n "$(find /data -xdev \( ! -user pnpb -o ! -group pnpb \) -print -quit)" ]; then
        echo "entrypoint: repairing /data ownership (files not owned by pnpb)" >&2
        chown -R pnpb:pnpb /data
    fi
    exec setpriv --reuid=pnpb --regid=pnpb --clear-groups \
        uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
