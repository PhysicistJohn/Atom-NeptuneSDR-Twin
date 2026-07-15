#!/usr/bin/env bash
# Non-destructive host and delivered-unit inventory for NeptuneSDR/P210.
# This script has no flash, DFU, RF streaming, RF TX, register-write, or IIO-write path.

set -u
set -o pipefail

ORIGINAL_ARGS=("$@")

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
UTC_STAMP=$(date -u +%Y%m%dT%H%M%SZ 2>/dev/null || date +%Y%m%dT%H%M%S)

OUTPUT="$REPO_ROOT/evidence/captures/$UTC_STAMP"
UNIT_ID="unassigned"
IIO_URI=""
SSH_HOST=""
SSH_PORT="22"
USB_ID="0456:b673"
ACCEPT_NEW_HOST_KEY=0
INTERACTIVE_SSH=0

usage() {
    cat <<'EOF'
Usage: capture_unit.sh [options]

Read-only capture of host USB state, libiio metadata, and optional SSH facts.
It never flashes, enters DFU, streams I/Q, enables RF TX, or writes IIO/device state.

Options:
  --output DIR             New or empty local capture directory
  --unit-id ID             Operator-assigned non-sensitive unit label
  --usb-id VID:PID         Extra lsusb verbose selector (default 0456:b673)
  --iio-uri URI            Read context metadata with iio_info -u URI
  --ssh-host [USER@]HOST   Run the fixed read-only remote inventory
  --ssh-port PORT          SSH port (default 22)
  --accept-new-host-key    Store a newly seen key in DIR/ssh-known-hosts
  --interactive-ssh        Permit SSH password/passphrase/host-key prompts
  -h, --help               Show this help

SSH is noninteractive by default (BatchMode=yes) and trusts only an existing
global host key. --accept-new-host-key uses a capture-local known_hosts file.
--interactive-ssh never records a password, but the operator must verify prompts.

Captures may contain serials, MAC addresses, IP addresses, hostnames, and details
of unrelated USB devices. Review or create a separately hashed redacted copy
before publication.
EOF
}

die() {
    printf 'capture_unit.sh: %s\n' "$*" >&2
    exit 2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --output)
            [ "$#" -ge 2 ] || die "--output requires a directory"
            OUTPUT=$2
            shift 2
            ;;
        --unit-id)
            [ "$#" -ge 2 ] || die "--unit-id requires a value"
            UNIT_ID=$2
            shift 2
            ;;
        --usb-id)
            [ "$#" -ge 2 ] || die "--usb-id requires VID:PID"
            USB_ID=$2
            shift 2
            ;;
        --iio-uri)
            [ "$#" -ge 2 ] || die "--iio-uri requires a URI"
            IIO_URI=$2
            shift 2
            ;;
        --ssh-host)
            [ "$#" -ge 2 ] || die "--ssh-host requires [USER@]HOST"
            SSH_HOST=$2
            shift 2
            ;;
        --ssh-port)
            [ "$#" -ge 2 ] || die "--ssh-port requires a port"
            SSH_PORT=$2
            shift 2
            ;;
        --accept-new-host-key)
            ACCEPT_NEW_HOST_KEY=1
            shift
            ;;
        --interactive-ssh)
            INTERACTIVE_SSH=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

case "$USB_ID" in
    [0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]:[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]) ;;
    *) die "--usb-id must look like 0456:b673" ;;
esac

case "$SSH_PORT" in
    ''|*[!0-9]*) die "--ssh-port must be numeric" ;;
esac
[ "$SSH_PORT" -le 65535 ] || die "--ssh-port must be at most 65535"
case "$SSH_HOST" in
    -*) die "--ssh-host cannot begin with '-'" ;;
esac

if [ -e "$OUTPUT" ]; then
    [ -d "$OUTPUT" ] || die "output exists and is not a directory: $OUTPUT"
    if [ -n "$(ls -A "$OUTPUT" 2>/dev/null)" ]; then
        die "refusing to mix evidence into non-empty directory: $OUTPUT"
    fi
else
    mkdir -p -- "$OUTPUT" || die "cannot create output directory: $OUTPUT"
fi

quote_command() {
    local item
    for item in "$@"; do
        printf '%q ' "$item"
    done
}

capture_cmd() {
    local filename=$1
    shift
    {
        printf 'command: '
        quote_command "$@"
        printf '\nstarted_utc: %s\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date)"
        "$@"
        local status=$?
        printf '\nexit_status: %d\nfinished_utc: %s\n' "$status" "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date)"
    } >"$OUTPUT/$filename" 2>&1
    return 0
}

mark_missing() {
    printf '%s\n' "$1" >>"$OUTPUT/unavailable-tools.txt"
}

script_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$0" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$0" | awk '{print $1}'
    elif command -v openssl >/dev/null 2>&1; then
        openssl dgst -sha256 "$0" | awk '{print $NF}'
    else
        printf 'unavailable'
    fi
}

{
    printf 'schema: 1\n'
    printf 'purpose: read-only delivered-unit inventory\n'
    printf 'unit_id: %s\n' "$UNIT_ID"
    printf 'started_utc: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date)"
    printf 'output: %s\n' "$OUTPUT"
    printf 'script_path: %s\n' "$0"
    printf 'script_sha256: %s\n' "$(script_sha256)"
    printf 'repository_root: %s\n' "$REPO_ROOT"
    if command -v git >/dev/null 2>&1 && git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        if REPOSITORY_COMMIT=$(git -C "$REPO_ROOT" rev-parse --verify HEAD 2>/dev/null); then
            printf 'repository_commit: %s\n' "$REPOSITORY_COMMIT"
        else
            printf 'repository_commit: unborn-or-unavailable\n'
        fi
        printf 'repository_dirty: '
        if [ -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null)" ]; then
            printf 'true\n'
        else
            printf 'false\n'
        fi
    else
        printf 'repository_commit: unavailable\n'
    fi
    printf 'usb_selector: %s\n' "$USB_ID"
    printf 'iio_uri: %s\n' "${IIO_URI:-not-requested}"
    printf 'ssh_host: %s\n' "${SSH_HOST:-not-requested}"
    printf 'ssh_interactive: %s\n' "$INTERACTIVE_SSH"
    printf 'accept_new_host_key: %s\n' "$ACCEPT_NEW_HOST_KEY"
    printf 'prohibited_actions: flash,DFU,RF-TX,IIO-write,register-write,sample-stream\n'
    printf 'invocation: '
    quote_command "$0" "${ORIGINAL_ARGS[@]}"
    printf '\n'
} >"$OUTPUT/run-metadata.txt"

# Preserve the exact collector used. This is a local evidence write only.
cp -p -- "$0" "$OUTPUT/capture-script.sh" 2>/dev/null || true

capture_cmd host-uname.txt uname -a
{
    printf 'shell: %s\n' "${SHELL:-unknown}"
    printf 'bash_version: %s\n' "${BASH_VERSION:-unknown}"
    printf 'locale: %s\n' "${LC_ALL:-${LANG:-unknown}}"
    printf 'Note: the complete process environment is intentionally not captured because it may contain credentials.\n'
} >"$OUTPUT/host-runtime.txt"

if command -v sw_vers >/dev/null 2>&1; then
    capture_cmd host-sw-vers.txt sw_vers
fi

if command -v ip >/dev/null 2>&1; then
    capture_cmd host-network-address.txt ip address show
    capture_cmd host-network-route.txt ip route show
elif command -v ifconfig >/dev/null 2>&1; then
    capture_cmd host-network-ifconfig.txt ifconfig -a
else
    mark_missing "ip/ifconfig"
fi

if command -v lsusb >/dev/null 2>&1; then
    capture_cmd usb-list.txt lsusb
    capture_cmd usb-topology.txt lsusb -t
    capture_cmd usb-selected-verbose.txt lsusb -v -d "$USB_ID"
    capture_cmd usb-reference-dfu-verbose.txt lsusb -v -d 0456:b674
else
    mark_missing "lsusb"
fi

if command -v system_profiler >/dev/null 2>&1; then
    capture_cmd usb-system-profiler.txt system_profiler SPUSBDataType -detailLevel full
else
    mark_missing "system_profiler"
fi

# Useful when run from Git Bash/MSYS on Windows. No device properties are changed.
if command -v powershell.exe >/dev/null 2>&1; then
    capture_cmd usb-windows-pnp.txt powershell.exe -NoLogo -NoProfile -NonInteractive -Command \
        'Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like "USB*" } | Sort-Object InstanceId | Format-List Status,Class,FriendlyName,InstanceId'
fi

if command -v iio_info >/dev/null 2>&1; then
    capture_cmd iio-version.txt iio_info --version
    capture_cmd iio-context-scan.txt iio_info -s
    if [ -n "$IIO_URI" ]; then
        capture_cmd iio-context-detail.txt iio_info -u "$IIO_URI"
    fi
else
    mark_missing "iio_info"
fi

capture_ssh() {
    local -a options
    options=(-T -p "$SSH_PORT" -o ConnectTimeout=8 -o ConnectionAttempts=1)
    if [ "$INTERACTIVE_SSH" -eq 0 ]; then
        options+=(-o BatchMode=yes)
    fi
    if [ "$ACCEPT_NEW_HOST_KEY" -eq 1 ]; then
        options+=(-o "UserKnownHostsFile=$OUTPUT/ssh-known-hosts" -o StrictHostKeyChecking=accept-new)
    else
        options+=(-o StrictHostKeyChecking=yes)
    fi

    {
        printf 'command: ssh [fixed read-only inventory] %s\n' "$SSH_HOST"
        printf 'started_utc: %s\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date)"
        ssh "${options[@]}" "$SSH_HOST" sh -s <<'REMOTE_READ_ONLY'
set +e

section() {
    printf '\n===== %s =====\n' "$1"
}

show_file() {
    if [ -r "$1" ]; then
        printf '%s:\n' "$1"
        cat "$1"
        printf '\n'
    fi
}

show_dt_file() {
    if [ -r "$1" ]; then
        printf '%s:\n' "$1"
        tr '\000' '\n' <"$1"
        printf '\n'
    fi
}

section identity
date -u 2>/dev/null || date
uname -a
show_file /etc/os-release
show_file /proc/version
show_file /proc/cpuinfo
show_file /proc/meminfo
show_file /proc/cmdline
show_file /proc/uptime

section clocks_read_only
show_file /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq
show_file /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq
show_file /sys/kernel/debug/clk/clk_summary

section device_tree_identity
show_dt_file /sys/firmware/devicetree/base/model
show_dt_file /sys/firmware/devicetree/base/compatible
show_dt_file /sys/firmware/devicetree/base/chosen/bootargs
if command -v find >/dev/null 2>&1; then
    find /sys/firmware/devicetree/base -maxdepth 4 -type f \( -name compatible -o -name status -o -name reg \) -print 2>/dev/null | sort
fi

section storage_layout_read_only
show_file /proc/mtd
show_file /proc/partitions
ls -l /dev/mtd* /dev/mmcblk* 2>/dev/null
mount 2>/dev/null
if command -v lsblk >/dev/null 2>&1; then
    lsblk -a -o NAME,KNAME,TYPE,SIZE,RO,FSTYPE,LABEL,UUID,MOUNTPOINT 2>/dev/null
fi

section boot_environment_read_only
if command -v fw_printenv >/dev/null 2>&1; then
    fw_printenv 2>&1
else
    printf 'fw_printenv unavailable\n'
fi

section ordinary_boot_file_hashes
hash_one() {
    if [ ! -f "$1" ] || [ ! -r "$1" ]; then
        return
    fi
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1"
    elif command -v openssl >/dev/null 2>&1; then
        openssl dgst -sha256 "$1"
    else
        ls -ln "$1"
    fi
}
for path in \
    /boot/BOOT.BIN /boot/uImage /boot/devicetree.dtb /boot/uEnv.txt \
    /mnt/BOOT/BOOT.BIN /mnt/BOOT/uImage /mnt/BOOT/devicetree.dtb /mnt/BOOT/uEnv.txt \
    /media/BOOT/BOOT.BIN /media/BOOT/uImage /media/BOOT/devicetree.dtb /media/BOOT/uEnv.txt
do
    hash_one "$path"
done

section iio_metadata_read_only
if command -v iio_info >/dev/null 2>&1; then
    iio_info --version 2>&1
    iio_info -s 2>&1
    iio_info -u local: 2>&1
fi
for directory in /sys/bus/iio/devices/iio:device*; do
    [ -d "$directory" ] || continue
    printf '\n[%s]\n' "$directory"
    for attribute in name label dev in_voltage_sampling_frequency_available out_voltage_sampling_frequency_available; do
        show_file "$directory/$attribute"
    done
    find "$directory/scan_elements" -maxdepth 1 -type f -print 2>/dev/null | sort
done

section usb_and_network
if command -v lsusb >/dev/null 2>&1; then
    lsusb 2>&1
    lsusb -t 2>&1
fi
if command -v ip >/dev/null 2>&1; then
    ip address show 2>&1
    ip route show 2>&1
elif command -v ifconfig >/dev/null 2>&1; then
    ifconfig -a 2>&1
fi
if command -v ethtool >/dev/null 2>&1; then
    for interface in /sys/class/net/*; do
        [ -d "$interface" ] || continue
        ethtool "${interface##*/}" 2>&1
    done
fi

section loaded_modules_and_messages
show_file /proc/modules
dmesg 2>&1

section usb_gadget_metadata_read_only
if command -v find >/dev/null 2>&1; then
    find /sys/kernel/config/usb_gadget -maxdepth 5 -type f -print 2>/dev/null | sort
fi

section completion
printf 'No flash, DFU, RF TX, IIO write, register write, or sample-stream command was issued.\n'
REMOTE_READ_ONLY
        local status=$?
        printf '\nssh_exit_status: %d\nfinished_utc: %s\n' "$status" "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date)"
    } >"$OUTPUT/ssh-read-only-inventory.txt" 2>&1
}

if [ -n "$SSH_HOST" ]; then
    if command -v ssh >/dev/null 2>&1; then
        capture_ssh
    else
        mark_missing "ssh"
    fi
fi

if [ ! -s "$OUTPUT/unavailable-tools.txt" ]; then
    rm -f -- "$OUTPUT/unavailable-tools.txt"
fi

{
    printf 'This directory is a read-only-at-source inventory.\n'
    printf 'The collector wrote local evidence files but issued no device write, flash, DFU, RF TX, IIO write, register write, or sample-stream command.\n'
    printf 'Review for serials, MAC addresses, IP addresses, hostnames, and unrelated USB devices before publication.\n'
    printf 'Raw evidence should be retained; publish a separate redacted derivative.\n'
} >"$OUTPUT/README.txt"

make_manifest() {
    local relative
    : >"$OUTPUT/SHA256SUMS"
    while IFS= read -r relative; do
        relative=${relative#./}
        [ "$relative" = "SHA256SUMS" ] && continue
        if command -v sha256sum >/dev/null 2>&1; then
            (cd "$OUTPUT" && sha256sum "$relative") >>"$OUTPUT/SHA256SUMS"
        elif command -v shasum >/dev/null 2>&1; then
            (cd "$OUTPUT" && shasum -a 256 "$relative") >>"$OUTPUT/SHA256SUMS"
        elif command -v openssl >/dev/null 2>&1; then
            printf '%s  %s\n' "$(openssl dgst -sha256 "$OUTPUT/$relative" | awk '{print $NF}')" "$relative" >>"$OUTPUT/SHA256SUMS"
        else
            printf 'UNAVAILABLE  %s\n' "$relative" >>"$OUTPUT/SHA256SUMS"
        fi
    done < <(cd "$OUTPUT" && find . -type f -print | LC_ALL=C sort)
}

make_manifest
printf 'Capture complete: %s\n' "$OUTPUT"
printf 'Review README.txt and verify SHA256SUMS before archiving.\n'
