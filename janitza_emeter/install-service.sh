#!/bin/sh
# Install janitza-emeter as a boot service (systemd preferred).
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
UNIT_SRC="$DIR/janitza-emeter.service"
INIT_SRC="$DIR/janitza-emeter.init"

if command -v systemctl >/dev/null 2>&1; then
    pkill -f '/janitza_emeter/janitza_emeter.py' 2>/dev/null || true
    cp "$UNIT_SRC" /etc/systemd/system/janitza-emeter.service
    systemctl daemon-reload
    systemctl enable --now janitza-emeter.service
    systemctl --no-pager --full status janitza-emeter.service
    exit 0
fi

if [ -d /etc/init.d ]; then
    cp "$INIT_SRC" /etc/init.d/janitza-emeter
    chmod 755 /etc/init.d/janitza-emeter
    if command -v update-rc.d >/dev/null 2>&1; then
        update-rc.d janitza-emeter defaults
    fi
    /etc/init.d/janitza-emeter start
    exit 0
fi

echo "Neither systemd nor /etc/init.d found" >&2
exit 1
