#!/bin/sh
# qemu-system-arm-compatible entry point for the repo-local UTM runtime.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CACHE=${QEMU_RUNTIME_CACHE:-"$ROOT/.cache/qemu-runtime"}
APP="$CACHE/utm-4.7.5/UTM.app"
LAUNCHER="$APP/Contents/XPCServices/QEMUHelper.xpc/Contents/MacOS/QEMULauncher.app/Contents/MacOS/QEMULauncher"
QEMU="$APP/Contents/Frameworks/qemu-arm-softmmu.framework/Versions/A/qemu-arm-softmmu"

if [ ! -x "$LAUNCHER" ] || [ ! -f "$QEMU" ]; then
    echo "error: repo-local QEMU is absent; run scripts/build_qemu.sh" >&2
    exit 127
fi

exec "$LAUNCHER" "$QEMU" "$@"

