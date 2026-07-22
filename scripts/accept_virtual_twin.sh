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
  --source-only       Run the source gate with exactly 15 explicit QEMU skips
  --reference-only    Deprecated alias for --source-only
  -h, --help          Show this help
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-build) BUILD=no; shift ;;
        --source-only|--reference-only) FIRMWARE=no; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

RUN_ID=$(date -u '+%Y%m%dT%H%M%SZ')-$$
OUTPUT_BASE=$OUTPUT
OUTPUT="$OUTPUT_BASE/runs/$RUN_ID"
python3 "$ROOT/scripts/acceptance_gate.py" start \
    --root "$ROOT" --output "$OUTPUT" --run-id "$RUN_ID" \
    --mode "$(if [ "$FIRMWARE" = yes ]; then printf full; else printf source; fi)"
printf '%s\n' RUNNING >"$OUTPUT/status"
acceptance_done=no
record_failure() {
    result=$?
    if [ "$acceptance_done" != yes ]; then
        printf 'FAILED exit=%s\n' "$result" >"$OUTPUT/status"
    fi
}
trap record_failure EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
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

    QEMU_CACHE=${P210_QEMU_CACHE:-"$ROOT/.cache/qemu-p210"}
    QEMU_BINARY=${P210_QEMU_BINARY:-"$QEMU_CACHE/bin/qemu-system-arm"}
    QEMU_BUILD_DIR=${P210_QEMU_BUILD_DIR:-"$QEMU_CACHE/src/qemu-10.0.2/build-p210"}
    test -x "$QEMU_BINARY"
    test -f "$QEMU_BUILD_DIR/compile_commands.json"
    python3 - "$QEMU_BINARY" "$QEMU_CACHE/bin/qemu-system-arm" \
        "$QEMU_BUILD_DIR/qemu-system-arm" <<'PY'
import os
import sys
if not os.path.samefile(sys.argv[1], sys.argv[2]) or not os.path.samefile(sys.argv[1], sys.argv[3]):
    raise SystemExit("configured, cached, and build-directory QEMU binaries are not identical")
PY
    "$QEMU_BINARY" --version | grep -Fq 'QEMU emulator version 10.0.2'
    "$QEMU_BINARY" -machine xilinx-zynq-a9,help 2>&1 |
        grep -q 'p210=<bool>.*Enable HAMGEEK P210 SDR devices'
    export P210_QEMU_BINARY="$QEMU_BINARY"
    export P210_QEMU_BUILD_DIR="$QEMU_BUILD_DIR"
else
    # Never let source-only acceptance consume a coincidental or stale cache.
    NO_QEMU="$OUTPUT/no-qemu"
    mkdir "$NO_QEMU"
    export P210_QEMU_BINARY="$NO_QEMU/qemu-system-arm"
    export P210_QEMU_BUILD_DIR="$NO_QEMU"
fi

python3 -m compileall -q "$ROOT/src" "$ROOT/scripts" "$ROOT/tests" \
    "$ROOT/cosim/tests"
python3 "$ROOT/scripts/acceptance_gate.py" test-suite \
    --start-dir "$ROOT/tests" --label acceptance-reference \
    --summary "$OUTPUT/reference-tests.json" \
    --log "$OUTPUT/reference-tests.log" \
    --expect-skips 0 --min-tests 46
if [ "$FIRMWARE" = yes ]; then
    python3 "$ROOT/scripts/acceptance_gate.py" test-suite \
        --start-dir "$ROOT/cosim/tests" --label acceptance-live-cosim \
        --summary "$OUTPUT/cosim-tests.json" \
        --log "$OUTPUT/cosim-tests.log" \
        --expect-skips 0 --min-tests 22 \
        --require-test test_qemu_device_sources.QEMUDeviceSourceTests.test_sources_compile_with_qemu_10_flags \
        --require-test test_qemu_fft_source.P210FFTSourceTests.test_integrated_qemu_executes_65536_bins_for_two_channels \
        --require-test test_qemu_fft_source.P210FFTSourceTests.test_integrated_qemu_executes_fft_and_rejects_overlap \
        --require-test test_qemu_sdr_live.P210SDRLiveTests.test_xsa_capabilities_alignment_and_pause_readback
else
    python3 "$ROOT/scripts/acceptance_gate.py" test-suite \
        --start-dir "$ROOT/cosim/tests" --label acceptance-source-cosim \
        --summary "$OUTPUT/cosim-tests.json" \
        --log "$OUTPUT/cosim-tests.log" \
        --expect-skips 15 \
        --expect-skip-reason '2:set P210_QEMU_BUILD_DIR to a configured QEMU 10.0.2 build' \
        --expect-skip-reason '13:set P210_QEMU_BINARY to an integrated P210 QEMU binary' \
        --skip-policy source-without-qemu \
        --min-tests 22
fi

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
    export P210_RUNTIME_OUTPUT="$OUTPUT/runtime"
    if [ "$BUILD" = yes ]; then
        "$ROOT/scripts/run_p210_firmware.sh" \
            >"$OUTPUT/firmware-runtime.log" 2>&1
    else
        "$ROOT/scripts/run_p210_firmware.sh" --no-build \
            >"$OUTPUT/firmware-runtime.log" 2>&1
    fi
    grep -Fxq 'P210_RUNTIME PASS' "$OUTPUT/firmware-runtime.log"
fi

if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$ROOT" diff --check
fi

if [ "$FIRMWARE" = yes ]; then
    python3 "$ROOT/scripts/acceptance_gate.py" finish-full \
        --root "$ROOT" --output "$OUTPUT" \
        --reference-summary "$OUTPUT/reference-tests.json" \
        --cosim-summary "$OUTPUT/cosim-tests.json" \
        --qemu "$QEMU_BINARY" --qemu-build-dir "$QEMU_BUILD_DIR" \
        --firmware-log "$OUTPUT/firmware-runtime.log" \
        --runtime-dir "$OUTPUT/runtime"
    printf '%s\n' PASS >"$OUTPUT/status"
    acceptance_done=yes
    printf '%s\n' "$RUN_ID" >"$OUTPUT_BASE/latest-pass.part"
    mv "$OUTPUT_BASE/latest-pass.part" "$OUTPUT_BASE/latest-pass"
    printf '%s\n' 'NEPTUNE_TWIN_ACCEPTANCE PASS'
    printf 'manifest=%s\n' "$OUTPUT/acceptance-manifest.json"
else
    printf '%s\n' 'SOURCE_ONLY_PASS_WITH_15_QEMU_SKIPS' >"$OUTPUT/status"
    acceptance_done=yes
    printf '%s\n' 'NEPTUNE_TWIN_SOURCE_ACCEPTANCE PASS skipped=15'
fi
printf 'evidence=%s\n' "$OUTPUT"
