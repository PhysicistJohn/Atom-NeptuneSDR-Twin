#!/bin/sh
# Reproduce the complete pre-arrival acceptance matrix and retain machine logs.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUTPUT=${P210_ACCEPTANCE_OUTPUT:-"$ROOT/.cache/acceptance"}
BUILD=yes
FIRMWARE=yes

usage() {
    cat <<'EOF'
Usage: accept_virtual_twin.sh [OPTIONS]

Run reference, protocol, source/co-simulation, locked-artifact and full ARM
firmware acceptance. The default may build QEMU/toolchains and download only
hash-locked public inputs; it never flashes hardware.

Options:
  --no-build          Reuse an existing P210 QEMU/ARM/host-libiio build
  --reference-only    Skip the firmware-executing QEMU acceptance
  -h, --help          Show this help
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-build) BUILD=no; shift ;;
        --reference-only) FIRMWARE=no; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

mkdir -p "$OUTPUT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

# The live qtests discover the pinned executable. Build it before the cosim
# suite on a fresh checkout so they cannot be silently skipped in a full run.
if [ "$FIRMWARE" = yes ]; then
    if [ "$BUILD" = yes ]; then
        "$ROOT/scripts/build_p210_qemu.sh" \
            >"$OUTPUT/qemu-build.log" 2>&1
    else
        test -x "${P210_QEMU_CACHE:-$ROOT/.cache/qemu-p210}/bin/qemu-system-arm"
    fi
fi

python3 -m compileall -q "$ROOT/src" "$ROOT/scripts" "$ROOT/tests" \
    "$ROOT/cosim/tests"
python3 -m unittest discover -s "$ROOT/tests" -v \
    >"$OUTPUT/reference-tests.log" 2>&1
python3 -m unittest discover -s "$ROOT/cosim/tests" -v \
    >"$OUTPUT/cosim-tests.log" 2>&1

for script in "$ROOT"/scripts/*.sh; do
    bash -n "$script"
done

python3 "$ROOT/scripts/test_firmware.py" --fetch --json \
    >"$OUTPUT/firmware-artifacts.json"
python3 -m neptunesdr_twin fetch-firmware p210-system-xsa \
    "$OUTPUT/system_top.xsa" >"$OUTPUT/xsa-fetch.json"
python3 -m neptunesdr_twin validate-firmware "$OUTPUT/system_top.xsa" \
    >"$OUTPUT/xsa-validation.json"
python3 -m neptunesdr_twin contracts >"$OUTPUT/contracts.json"
python3 -m neptunesdr_twin fft-plan >"$OUTPUT/fft-plan.json"
python3 -m neptunesdr_twin appliance --dry-run \
    >"$OUTPUT/appliance-plan.json"
"$ROOT/scripts/run_virtual_appliance.sh" --no-build --dry-run \
    >"$OUTPUT/firmware-appliance-plan.txt"

if [ "$FIRMWARE" = yes ]; then
    if [ "$BUILD" = yes ]; then
        "$ROOT/scripts/run_p210_firmware.sh" \
            >"$OUTPUT/firmware-runtime.log" 2>&1
    else
        "$ROOT/scripts/run_p210_firmware.sh" --no-build \
            >"$OUTPUT/firmware-runtime.log" 2>&1
    fi
    grep -Fq 'P210_RUNTIME PASS' "$OUTPUT/firmware-runtime.log"
fi

if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$ROOT" diff --check
fi

printf '%s\n' 'NEPTUNE_TWIN_ACCEPTANCE PASS'
printf 'evidence=%s\n' "$OUTPUT"
