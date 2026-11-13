#!/bin/sh
# PO-token server, bound to localhost only (yt-dlp's bgutil plugin finds it at 127.0.0.1:4416).
node /opt/bgutil-server/build/main.js &

# One worker: download jobs are tracked in memory, so they must share a process.
exec gunicorn --workers 1 --threads 16 --timeout 600 --bind "0.0.0.0:${PORT:-10000}" app:app
