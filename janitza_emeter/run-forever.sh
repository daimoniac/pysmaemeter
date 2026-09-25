#!/bin/sh
# Restart loop used when systemd/init is not yet installed.
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PYTHONUNBUFFERED=1
cd "$ROOT"
while true; do
    /usr/bin/python3 "$ROOT/janitza_emeter/janitza_emeter.py"
    sleep 5
done
