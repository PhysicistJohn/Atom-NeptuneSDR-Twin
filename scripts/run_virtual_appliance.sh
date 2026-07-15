#!/bin/sh
# Run the firmware-executing P210 VM and its host-visible USB/IP adapter as one
# bounded-lifecycle development appliance.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUTPUT=${P210_APPLIANCE_OUTPUT:-"$ROOT/.cache/p210-appliance"}
IIO_PORT=${P210_IIO_HOST_PORT:-30431}
FFT_PORT=${P210_FFT_HOST_PORT:-30432}
UART_PORT=${P210_UART_HOST_PORT:-30433}
GDB_PORT=
USBIP_HOST=127.0.0.1
USBIP_PORT=3240
TIMEOUT=180
BUILD=yes
DRY_RUN=no
SHUTDOWN_GRACE_SECONDS=5
KILL_GRACE_SECONDS=1

usage() {
    cat <<'EOF'
Usage: run_virtual_appliance.sh [OPTIONS]

Start the P210 QEMU firmware target, wait for released guest iiod and the ARM
FFT service, then export the observed Neptune composite device over USB/IP.
Both processes are stopped together on Ctrl-C or TERM.

Options:
  --no-build          Reuse existing QEMU, ARM streamer and host-libiio builds
  --timeout SECONDS   Readiness timeout passed to the firmware target (default: 180)
  --usbip-host HOST   USB/IP listen address (default: 127.0.0.1)
  --usbip-port PORT   USB/IP listen port (default: 3240)
  --uart-port PORT    Loopback UART1 console port (default: 30433)
  --gdb PORT          Opt-in loopback QEMU GDB endpoint; disabled by default
  --dry-run           Print the resolved appliance without opening listeners
  -h, --help          Show this help

The real guest endpoints use P210_IIO_HOST_PORT (default 30431) and
P210_FFT_HOST_PORT (default 30432); UART uses P210_UART_HOST_PORT (default
30433). Set --usbip-host 0.0.0.0 only when a trusted remote Linux host needs
to attach.
EOF
}

die() {
    printf 'run_virtual_appliance.sh: %s\n' "$*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-build) BUILD=no; shift ;;
        --timeout)
            [ "$#" -ge 2 ] || die '--timeout requires a value'
            TIMEOUT=$2
            shift 2
            ;;
        --usbip-host)
            [ "$#" -ge 2 ] || die '--usbip-host requires a value'
            USBIP_HOST=$2
            shift 2
            ;;
        --usbip-port)
            [ "$#" -ge 2 ] || die '--usbip-port requires a value'
            USBIP_PORT=$2
            shift 2
            ;;
        --uart-port)
            [ "$#" -ge 2 ] || die '--uart-port requires a value'
            UART_PORT=$2
            shift 2
            ;;
        --gdb)
            [ "$#" -ge 2 ] || die '--gdb requires a port'
            GDB_PORT=$2
            shift 2
            ;;
        --dry-run) DRY_RUN=yes; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

case "$TIMEOUT:$IIO_PORT:$FFT_PORT:$UART_PORT:$USBIP_PORT${GDB_PORT:+:$GDB_PORT}" in
    *[!0-9:]*) die 'timeout and ports must be decimal integers' ;;
esac
[ "$TIMEOUT" -gt 0 ] || die '--timeout must be positive'
for port in "$IIO_PORT" "$FFT_PORT" "$UART_PORT" "$USBIP_PORT"; do
    [ "$port" -gt 0 ] && [ "$port" -le 65535 ] || die "invalid port: $port"
done
[ -z "$GDB_PORT" ] || {
    [ "$GDB_PORT" -gt 0 ] && [ "$GDB_PORT" -le 65535 ] || die 'invalid GDB port'
}
ports="$IIO_PORT $FFT_PORT $UART_PORT $USBIP_PORT${GDB_PORT:+ $GDB_PORT}"
seen=
for port in $ports; do
    case " $seen " in
        *" $port "*) die 'iiod, FFT, UART, USB/IP, and GDB ports must differ' ;;
    esac
    seen="$seen $port"
done
[ -n "$USBIP_HOST" ] || die '--usbip-host must not be empty'

build_option=
[ "$BUILD" = yes ] || build_option=--no-build

if [ "$DRY_RUN" = yes ]; then
    printf 'firmware=%s/scripts/run_p210_firmware.sh --serve %s --timeout %s --uart-port %s%s\n' \
        "$ROOT" "$build_option" "$TIMEOUT" "$UART_PORT" \
        "${GDB_PORT:+ --gdb $GDB_PORT}"
    printf 'iiod=ip:127.0.0.1:%s\n' "$IIO_PORT"
    printf 'fft=tcp:127.0.0.1:%s\n' "$FFT_PORT"
    printf 'uart=tcp:127.0.0.1:%s\n' "$UART_PORT"
    [ -z "$GDB_PORT" ] || printf 'gdb=tcp:127.0.0.1:%s\n' "$GDB_PORT"
    printf 'usbip=%s:%s busid=1-1 backend=127.0.0.1:%s\n' \
        "$USBIP_HOST" "$USBIP_PORT" "$IIO_PORT"
    exit 0
fi

mkdir -p "$OUTPUT"
FIRMWARE_LOG="$OUTPUT/firmware-service.log"
USBIP_LOG="$OUTPUT/usbip-service.log"
: >"$FIRMWARE_LOG"
: >"$USBIP_LOG"

firmware_pid=
usbip_pid=
process_live() {
    [ -n "$1" ] || return 1
    kill -0 "$1" 2>/dev/null || return 1
    state=$(ps -p "$1" -o stat= 2>/dev/null | awk 'NR == 1 { print $1 }')
    case "$state" in
        ''|Z*) return 1 ;;
        *) return 0 ;;
    esac
}

request_process_stop() {
    target_pid=$1
    [ -z "$target_pid" ] || kill -TERM "$target_pid" 2>/dev/null || true
}

reap_process_bounded() {
    target_pid=$1
    label=$2
    [ -n "$target_pid" ] || return 0

    stop_deadline=$(($(date +%s) + SHUTDOWN_GRACE_SECONDS))
    while process_live "$target_pid" && [ "$(date +%s)" -lt "$stop_deadline" ]; do
        sleep 0.1
    done
    if process_live "$target_pid"; then
        printf 'run_virtual_appliance.sh: %s ignored TERM; sending KILL\n' "$label" >&2
        kill -KILL "$target_pid" 2>/dev/null || true
        kill_deadline=$(($(date +%s) + KILL_GRACE_SECONDS))
        while process_live "$target_pid" && [ "$(date +%s)" -lt "$kill_deadline" ]; do
            sleep 0.1
        done
    fi
    if process_live "$target_pid"; then
        printf 'run_virtual_appliance.sh: %s remains uninterruptible; not waiting forever\n' \
            "$label" >&2
        return 0
    fi
    wait "$target_pid" 2>/dev/null || true
}

cleanup() {
    # Prevent a repeated signal or the explicit exit below from re-entering the
    # EXIT trap halfway through process ownership teardown.
    trap - 0
    trap '' 2 15
    # Signal both first so their independent graceful-shutdown clocks overlap.
    request_process_stop "$usbip_pid"
    request_process_stop "$firmware_pid"
    reap_process_bounded "$usbip_pid" 'USB/IP adapter'
    reap_process_bounded "$firmware_pid" 'firmware target'
    usbip_pid=
    firmware_pid=
}
trap cleanup 0
trap 'exit 130' 2
trap 'exit 143' 15

set -- "$ROOT/scripts/run_p210_firmware.sh" --serve --timeout "$TIMEOUT" \
    --uart-port "$UART_PORT"
[ -z "$build_option" ] || set -- "$@" "$build_option"
[ -z "$GDB_PORT" ] || set -- "$@" --gdb "$GDB_PORT"
"$@" >"$FIRMWARE_LOG" 2>&1 &
firmware_pid=$!

deadline=$(($(date +%s) + TIMEOUT))
while ! grep -Fq 'P210_RUNTIME READY' "$FIRMWARE_LOG"; do
    if ! process_live "$firmware_pid"; then
        cat "$FIRMWARE_LOG" >&2
        die 'firmware target exited before readiness'
    fi
    [ "$(date +%s)" -lt "$deadline" ] || {
        tail -100 "$FIRMWARE_LOG" >&2
        die "timed out after ${TIMEOUT}s waiting for firmware target"
    }
    sleep 0.2
done

PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m neptunesdr_twin usbip-serve \
    --host "$USBIP_HOST" \
    --port "$USBIP_PORT" \
    --iiod-backend "127.0.0.1:$IIO_PORT" \
    >"$USBIP_LOG" 2>&1 &
usbip_pid=$!

deadline=$(($(date +%s) + TIMEOUT))
while ! grep -Fq 'NeptuneSDR USB/IP twin listening' "$USBIP_LOG"; do
    if ! process_live "$usbip_pid"; then
        cat "$USBIP_LOG" >&2
        die 'USB/IP adapter exited before readiness'
    fi
    if ! process_live "$firmware_pid"; then
        cat "$FIRMWARE_LOG" >&2
        die 'firmware target exited while starting USB/IP'
    fi
    [ "$(date +%s)" -lt "$deadline" ] || {
        cat "$USBIP_LOG" >&2
        die "timed out after ${TIMEOUT}s waiting for USB/IP adapter"
    }
    sleep 0.1
done

printf '%s\n' 'NEPTUNE_APPLIANCE READY'
printf 'iiod=ip:127.0.0.1:%s\n' "$IIO_PORT"
printf 'fft=tcp:127.0.0.1:%s\n' "$FFT_PORT"
printf 'uart=tcp:127.0.0.1:%s\n' "$UART_PORT"
[ -z "$GDB_PORT" ] || printf 'gdb=tcp:127.0.0.1:%s\n' "$GDB_PORT"
printf 'usbip=%s:%s busid=1-1\n' "$USBIP_HOST" "$USBIP_PORT"
printf 'firmware_log=%s\n' "$FIRMWARE_LOG"
printf 'usbip_log=%s\n' "$USBIP_LOG"
printf '%s\n' 'Press Ctrl-C to stop the complete appliance.'

while process_live "$firmware_pid" && process_live "$usbip_pid"; do
    sleep 0.5
done

if ! process_live "$firmware_pid"; then
    cat "$FIRMWARE_LOG" >&2
    die 'firmware target stopped unexpectedly'
fi
cat "$USBIP_LOG" >&2
die 'USB/IP adapter stopped unexpectedly'
