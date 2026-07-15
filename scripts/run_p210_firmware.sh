#!/bin/sh
# Boot the composed P210/Pluto ARM firmware on the native P210 QEMU machine.
# The default mode proves IIO-DMAC block capture -> ARM copy -> PL FFT DMA ->
# NSFT/TCP and always tears the VM down. --serve keeps it available until
# interrupted; neither mode claims a continuous zero-copy sample path.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
QEMU_CACHE=${P210_QEMU_CACHE:-"$ROOT/.cache/qemu-p210"}
RUNTIME=${P210_RUNTIME_OUTPUT:-"$ROOT/.cache/p210-runtime"}
GUEST=${P210_GUEST_OUTPUT:-"$ROOT/.cache/p210-guest/neptune-fft-streamer"}
FFT_TOOLCHAIN=${P210_FFT_TOOLCHAIN:-"$ROOT/.cache/fft-toolchain"}
QEMU="$QEMU_CACHE/bin/qemu-system-arm"
MAMBA="$QEMU_CACHE/tools/micromamba"
KERNEL="$RUNTIME/p210-kernel.bin"
DTB="$RUNTIME/p210-devicetree.dtb"
INITRD="$RUNTIME/qemu-fft-runtime.cpio.gz"
LOG=${P210_QEMU_LOG:-"$RUNTIME/p210-qemu.log"}
QEMU_STDERR=${P210_QEMU_STDERR:-"$RUNTIME/p210-qemu.stderr.log"}
CAPTURE=${P210_FFT_CAPTURE:-"$RUNTIME/p210-qemu-fft.nsft"}
REPORT=${P210_FFT_REPORT:-"$RUNTIME/p210-qemu-fft-report.json"}
IIO_PORT=${P210_IIO_HOST_PORT:-30431}
FFT_PORT=${P210_FFT_HOST_PORT:-30432}
IIO_REPORT=${P210_IIO_REPORT:-"$RUNTIME/p210-qemu-iio-info.txt"}
TIMEOUT=180
MODE=selftest
BUILD=yes
SHUTDOWN_GRACE_SECONDS=5

usage() {
    cat <<'EOF'
Usage: run_p210_firmware.sh [OPTIONS]

Build and boot the two-core P210 machine with the locked public P210 kernel,
the ABI-audited official Pluto v0.39 userspace, AD9361, IIO DMA, and the
65,536-point two-channel PL FFT accelerator.

The default is a bounded self-test of the VM: inspect the released guest iiod
with the pinned upstream host libiio client, receive and CRC-check one
synchronized two-channel NSFT update, enforce boot/runtime log gates, then stop
QEMU.
Dependency downloads/builds happen before the VM starts and are not covered by
--timeout.

Options:
  --serve             Keep iiod and the FFT service running until Ctrl-C
  --no-build          Reuse existing QEMU and guest streamer builds
  --timeout SECONDS   Per-phase VM readiness/capture timeout (default: 180)
  --log FILE          Guest serial log path
  --capture FILE      NSFT wire-capture path
  --iio-report FILE   Retained upstream iio_info output path
  -h, --help          Show this help

Endpoints while running:
  iiod TCP            127.0.0.1:30431 (override P210_IIO_HOST_PORT)
  NSFT FFT TCP        127.0.0.1:30432 (override P210_FFT_HOST_PORT)
EOF
}

die() {
    printf 'run_p210_firmware.sh: %s\n' "$*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --serve) MODE=serve; shift ;;
        --no-build) BUILD=no; shift ;;
        --timeout)
            [ "$#" -ge 2 ] || die '--timeout requires a value'
            TIMEOUT=$2
            shift 2
            ;;
        --log)
            [ "$#" -ge 2 ] || die '--log requires a path'
            LOG=$2
            shift 2
            ;;
        --capture)
            [ "$#" -ge 2 ] || die '--capture requires a path'
            CAPTURE=$2
            shift 2
            ;;
        --iio-report)
            [ "$#" -ge 2 ] || die '--iio-report requires a path'
            IIO_REPORT=$2
            shift 2
            ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

case "$TIMEOUT" in
    ''|*[!0-9]*) die '--timeout must be a positive integer' ;;
esac
[ "$TIMEOUT" -gt 0 ] || die '--timeout must be a positive integer'
case "$IIO_PORT:$FFT_PORT" in
    *[!0-9:]*) die 'host ports must be decimal integers' ;;
esac
[ "$IIO_PORT" -gt 0 ] && [ "$IIO_PORT" -le 65535 ] || die 'invalid iiod host port'
[ "$FFT_PORT" -gt 0 ] && [ "$FFT_PORT" -le 65535 ] || die 'invalid FFT host port'
[ "$IIO_PORT" != "$FFT_PORT" ] || die 'iiod and FFT host ports must differ'

if [ "$BUILD" = yes ]; then
    "$ROOT/scripts/build_p210_qemu.sh"

    if [ -n "${ZIG:-}" ]; then
        ZIG_BIN=$ZIG
    else
        ZIG_BIN="$FFT_TOOLCHAIN/bin/zig"
        if [ ! -x "$ZIG_BIN" ] || [ "$($ZIG_BIN version 2>/dev/null || true)" != 0.14.1 ]; then
            [ -x "$MAMBA" ] || die 'pinned micromamba was not provisioned by the QEMU build'
            if [ -d "$FFT_TOOLCHAIN/conda-meta" ]; then
                MAMBA_ROOT_PREFIX="$QEMU_CACHE/mamba-root" "$MAMBA" install -y \
                    -p "$FFT_TOOLCHAIN" -c conda-forge --strict-channel-priority \
                    zig=0.14.1
            elif [ -e "$FFT_TOOLCHAIN" ]; then
                die "FFT toolchain path exists but is not a conda environment: $FFT_TOOLCHAIN"
            else
                MAMBA_ROOT_PREFIX="$QEMU_CACHE/mamba-root" "$MAMBA" create -y \
                    -p "$FFT_TOOLCHAIN" -c conda-forge --strict-channel-priority \
                    zig=0.14.1
            fi
        fi
    fi
    ZIG="$ZIG_BIN" P210_GUEST_OUTPUT="$GUEST" "$ROOT/scripts/build_guest_fft.sh"
    "$ROOT/scripts/build_host_libiio.sh"
else
    [ -x "$QEMU" ] || die "P210 QEMU is absent: $QEMU"
    [ -x "$GUEST" ] || die "guest FFT streamer is absent: $GUEST"
    "$ROOT/scripts/build_host_libiio.sh" --verify
fi

python3 "$ROOT/scripts/fetch_firmware.py" p210-sd-boot plutosdr-fw-v0.39
python3 "$ROOT/scripts/prepare_runtime.py" \
    --output "$RUNTIME" \
    --fft-streamer "$GUEST"

for artifact in "$QEMU" "$KERNEL" "$DTB" "$INITRD"; do
    [ -f "$artifact" ] || die "runtime artifact is absent: $artifact"
done

mkdir -p "$(dirname -- "$LOG")" "$(dirname -- "$QEMU_STDERR")" \
    "$(dirname -- "$CAPTURE")" "$(dirname -- "$REPORT")" \
    "$(dirname -- "$IIO_REPORT")"
: >"$LOG"
: >"$QEMU_STDERR"
rm -f "$CAPTURE" "$REPORT" "$IIO_REPORT" \
    "$IIO_REPORT.part" "$IIO_REPORT.stderr.part"

qemu_pid=
qemu_is_live() {
    [ -n "$qemu_pid" ] || return 1
    kill -0 "$qemu_pid" 2>/dev/null || return 1
    state=$(ps -p "$qemu_pid" -o stat= 2>/dev/null | awk 'NR == 1 { print $1 }')
    case "$state" in
        ''|Z*) return 1 ;;
        *) return 0 ;;
    esac
}

cleanup() {
    rm -f "$IIO_REPORT.part" "$IIO_REPORT.stderr.part"
    if qemu_is_live; then
        kill "$qemu_pid" 2>/dev/null || true
        stop_deadline=$(($(date +%s) + SHUTDOWN_GRACE_SECONDS))
        while qemu_is_live && [ "$(date +%s)" -lt "$stop_deadline" ]; do
            sleep 0.1
        done
        if qemu_is_live; then
            printf 'run_p210_firmware.sh: QEMU ignored TERM; sending KILL\n' >&2
            kill -KILL "$qemu_pid" 2>/dev/null || true
        fi
    fi
    if [ -n "$qemu_pid" ]; then
        wait "$qemu_pid" 2>/dev/null || true
    fi
    qemu_pid=
}
trap cleanup 0
trap 'exit 130' 2
trap 'exit 143' 15

"$QEMU" \
    -machine xilinx-zynq-a9,p210=on \
    -cpu cortex-a9 \
    -m 512M \
    -smp 2 \
    -kernel "$KERNEL" \
    -dtb "$DTB" \
    -initrd "$INITRD" \
    -append 'console=ttyPS0,115200 earlycon=cdns,mmio,0xe0001000,115200n8 rdinit=/init rw loglevel=7 mem=384M' \
    -nic "user,hostfwd=tcp:127.0.0.1:${IIO_PORT}-10.0.2.15:30431,hostfwd=tcp:127.0.0.1:${FFT_PORT}-10.0.2.15:30432" \
    -display none \
    -serial null \
    -serial "file:$LOG" \
    -monitor none \
    -no-reboot \
    >"$QEMU_STDERR" 2>&1 &
qemu_pid=$!

REJECT='Division by zero|Kernel panic|Oops|BUG:|fatal=|Unhandled fault|Internal error|Call trace|attempted to kill init|segfault'
READY='NEPTUNE_FFT ready port=30432 n=65536 channels=2 input=iio-dmac-cpu-copy'
deadline=$(($(date +%s) + TIMEOUT))
while ! grep -Fq "$READY" "$LOG" 2>/dev/null; do
    if grep -Eq "$REJECT" "$LOG" 2>/dev/null; then
        tail -80 "$LOG" >&2
        die 'guest reported a rejected kernel/runtime condition'
    fi
    if ! kill -0 "$qemu_pid" 2>/dev/null; then
        wait "$qemu_pid" 2>/dev/null || true
        tail -80 "$LOG" >&2
        cat "$QEMU_STDERR" >&2
        die 'QEMU exited before the FFT service became ready'
    fi
    [ "$(date +%s)" -lt "$deadline" ] || {
        tail -80 "$LOG" >&2
        die "timed out after ${TIMEOUT}s waiting for FFT readiness"
    }
    sleep 0.2
done

required_boot_markers='
AD936x Rev 2 successfully initialized
Analog Devices CF_AXI_DDS_DDS MASTER (9.00.a)
ADI AIM (10.00.a)
NEPTUNE_RUNTIME cpu-online=0-1
NEPTUNE_FFT accelerator-id=5446464e version=00010000 caps=0000003f
NEPTUNE_FFT rf-bandwidth=50000000 sample-rate=61440000
NEPTUNE_FFT ready port=30432 n=65536 channels=2 input=iio-dmac-cpu-copy'
printf '%s\n' "$required_boot_markers" | while IFS= read -r marker; do
    [ -z "$marker" ] || grep -Fq "$marker" "$LOG" || die "missing log marker: $marker"
done

# A successful FFT socket says nothing about the independently launched guest
# iiod. Make the released daemon, forwarded GEM connection, and pinned official
# host library a required part of both self-test and long-running modes.
iio_uri="ip:127.0.0.1:${IIO_PORT}"
iio_ready=no
iio_deadline=$(($(date +%s) + TIMEOUT))
while [ "$(date +%s)" -lt "$iio_deadline" ]; do
    if "$ROOT/scripts/host_iio.sh" --uri "$iio_uri" info -T 1000 \
        >"$IIO_REPORT.part" 2>"$IIO_REPORT.stderr.part"; then
        mv "$IIO_REPORT.part" "$IIO_REPORT"
        rm -f "$IIO_REPORT.stderr.part"
        iio_ready=yes
        break
    fi
    if ! qemu_is_live; then
        tail -80 "$LOG" >&2
        cat "$QEMU_STDERR" >&2
        cat "$IIO_REPORT.stderr.part" >&2 2>/dev/null || true
        die 'QEMU exited before released guest iiod answered'
    fi
    sleep 0.2
done
[ "$iio_ready" = yes ] || {
    cat "$IIO_REPORT.stderr.part" >&2 2>/dev/null || true
    die "timed out after ${TIMEOUT}s waiting for released guest iiod"
}

required_iio_markers='
iio_info version: 0.26 (git tag:a0eca0d)
Backend version: 0.26 (git tag: v0.26)
IIO context has 5 devices:
iio:device0: ad9361-phy
cf-ad9361-lpc (buffer capable)
rf_bandwidth value: 50000000
sampling_frequency value: 61440000'
printf '%s\n' "$required_iio_markers" | while IFS= read -r marker; do
    [ -z "$marker" ] || grep -Fq "$marker" "$IIO_REPORT" || \
        die "released guest iiod is missing context marker: $marker"
done

audit_runtime_errors() {
    serious="$RUNTIME/.p210-runtime-serious.$$"
    novel="$RUNTIME/.p210-runtime-novel.$$"
    grep -Ehi 'warning|error|failed|unable|panic|oops|BUG:|fatal|division by zero|unhandled fault|internal error|call trace|segfault' \
        "$LOG" "$QEMU_STDERR" >"$serious" || true
    grep -Eiv \
        'jitterentropy: Initialization failed with host not compliant with requirements: 2|ci_hdrc ci_hdrc\.0: unable to init phy: -110|ci_hdrc: probe of ci_hdrc\.0 failed with error -110|adf4350 spi1\.[01]: Probe failed \(muxout\)|zynq_pm_suspend_init: Unable to map OCM\.|hctosys: unable to open rtc device \(rtc0\)|macb e000b000\.ethernet eth0: unable to generate target frequency: 25000000 Hz|qemu-system-arm: warning: nic cadence_gem\.1 has no peer' \
        "$serious" >"$novel" || true
    rm -f "$serious"
    if [ -s "$novel" ]; then
        printf '%s\n' 'Unexpected serious runtime diagnostics:' >&2
        cat "$novel" >&2
        rm -f "$novel"
        die 'guest/QEMU emitted a serious diagnostic outside the explicit allowlist'
    fi
    rm -f "$novel"
}

audit_runtime_errors

if [ "$MODE" = serve ]; then
    printf '%s\n' 'P210_RUNTIME READY'
    printf 'iiod=ip:127.0.0.1:%s\n' "$IIO_PORT"
    printf 'fft=tcp:127.0.0.1:%s\n' "$FFT_PORT"
    printf 'serial_log=%s\n' "$LOG"
    printf 'iio_report=%s\n' "$IIO_REPORT"
    printf '%s\n' 'Press Ctrl-C to stop the VM cleanly.'
    wait "$qemu_pid"
    exit $?
fi

python3 "$ROOT/scripts/capture_guest_fft.py" \
    --host 127.0.0.1 \
    --port "$FFT_PORT" \
    --timeout "$TIMEOUT" \
    --output "$CAPTURE" \
    >"$REPORT"

deadline=$(($(date +%s) + 10))
while ! grep -Eq 'NEPTUNE_FFT transmitted sequence=[0-9]+ bins=131072 bytes=262288' "$LOG"; do
    [ "$(date +%s)" -lt "$deadline" ] || die 'missing transmitted FFT acceptance marker'
    sleep 0.1
done
if grep -Eq "$REJECT" "$LOG"; then
    tail -80 "$LOG" >&2
    die 'guest reported a rejected kernel/runtime condition'
fi
grep -Fq 'macb e000b000.ethernet eth0: link up' "$LOG" || die 'Ethernet link did not come up'
audit_runtime_errors

cat "$REPORT"
printf '%s\n' 'P210_RUNTIME PASS'
printf 'serial_log=%s\n' "$LOG"
printf 'iio_report=%s\n' "$IIO_REPORT"
printf 'nsft_capture=%s\n' "$CAPTURE"
printf 'capture_report=%s\n' "$REPORT"
