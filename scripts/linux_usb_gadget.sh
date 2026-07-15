#!/usr/bin/env bash
# Linux configfs USB gadget skeleton for controlled P210 interface testing.
# Dry-run is the default. Only --apply permits any system mutation.

set -u
set -o pipefail

APPLY=0
BIND_EXISTING=0
ACK_OBSERVED_VID=0
NAME="neptune_p210_twin"
UDC=""
VID="0x0456"
PID="0xb673"
BCD_USB="0x0200"
BCD_DEVICE="0x0515"
SERIAL=""
SERIAL_SET=0
MANUFACTURER="NeptuneSDR Twin (development only)"
PRODUCT="P210 behavioral twin (not delivered hardware)"
DEVICE_MAC="02:50:21:00:00:01"
HOST_MAC="02:50:21:00:00:02"
ENABLE_RNDIS=1
ENABLE_ACM=1
MASS_STORAGE_IMAGE=""
FUNCTIONFS_MOUNT=""
CONFIGFS_ROOT="/sys/kernel/config"

usage() {
    cat <<'EOF'
Usage: linux_usb_gadget.sh [options]

Print a Linux configfs gadget plan. No command is executed unless --apply is
present. An applied skeleton is not descriptor-exact and is not a native-IIO
implementation by itself.

Core options:
  --apply                     Execute the displayed configuration on Linux
  --bind-existing             Bind a skeleton prepared in an earlier --apply run
  --acknowledge-observed-vid  Required to apply default ADI VID 0x0456
  --name NAME                 Configfs gadget name (default neptune_p210_twin)
  --udc NAME                  USB Device Controller; auto-select if exactly one
  --serial VALUE              Unique development serial (required with 0x0456)
  --vid 0xNNNN                Vendor ID (default observed 0x0456)
  --pid 0xNNNN                Product ID (default observed 0xb673)
  --manufacturer TEXT         USB manufacturer string
  --product TEXT              USB product string

Functions:
  --no-rndis                  Omit the RNDIS Ethernet function
  --no-acm                    Omit the CDC ACM serial function
  --device-mac XX:...         RNDIS device MAC
  --host-mac XX:...           RNDIS host MAC
  --mass-storage-image FILE   Add one forced-read-only removable LUN
  --functionfs-mount DIR      Prepare ffs.iio at DIR, but leave gadget unbound

FunctionFS is a two-stage operation:
  1. Run --apply --functionfs-mount DIR to create/mount the unbound skeleton.
  2. Start a userspace FunctionFS IIO service that writes descriptors/strings.
  3. Re-run with --apply --bind-existing --functionfs-mount DIR to verify
     ready=1, link ffs.iio into the configuration, and bind the UDC.

There is intentionally no teardown command. Inspect the gadget, unbind it by
writing an empty string to UDC, then remove links/functions deliberately.
EOF
}

die() {
    printf 'linux_usb_gadget.sh: %s\n' "$*" >&2
    exit 2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --apply)
            APPLY=1
            shift
            ;;
        --bind-existing)
            BIND_EXISTING=1
            shift
            ;;
        --acknowledge-observed-vid)
            ACK_OBSERVED_VID=1
            shift
            ;;
        --name)
            [ "$#" -ge 2 ] || die "--name requires a value"
            NAME=$2
            shift 2
            ;;
        --udc)
            [ "$#" -ge 2 ] || die "--udc requires a value"
            UDC=$2
            shift 2
            ;;
        --serial)
            [ "$#" -ge 2 ] || die "--serial requires a value"
            SERIAL=$2
            SERIAL_SET=1
            shift 2
            ;;
        --vid)
            [ "$#" -ge 2 ] || die "--vid requires 0xNNNN"
            VID=$2
            shift 2
            ;;
        --pid)
            [ "$#" -ge 2 ] || die "--pid requires 0xNNNN"
            PID=$2
            shift 2
            ;;
        --manufacturer)
            [ "$#" -ge 2 ] || die "--manufacturer requires text"
            MANUFACTURER=$2
            shift 2
            ;;
        --product)
            [ "$#" -ge 2 ] || die "--product requires text"
            PRODUCT=$2
            shift 2
            ;;
        --no-rndis)
            ENABLE_RNDIS=0
            shift
            ;;
        --no-acm)
            ENABLE_ACM=0
            shift
            ;;
        --device-mac)
            [ "$#" -ge 2 ] || die "--device-mac requires an address"
            DEVICE_MAC=$2
            shift 2
            ;;
        --host-mac)
            [ "$#" -ge 2 ] || die "--host-mac requires an address"
            HOST_MAC=$2
            shift 2
            ;;
        --mass-storage-image)
            [ "$#" -ge 2 ] || die "--mass-storage-image requires a file"
            MASS_STORAGE_IMAGE=$2
            shift 2
            ;;
        --functionfs-mount)
            [ "$#" -ge 2 ] || die "--functionfs-mount requires a directory"
            FUNCTIONFS_MOUNT=$2
            shift 2
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

[[ "$VID" =~ ^0x[0-9A-Fa-f]{4}$ ]] || die "--vid must look like 0x0456"
[[ "$PID" =~ ^0x[0-9A-Fa-f]{4}$ ]] || die "--pid must look like 0xb673"
[[ "$DEVICE_MAC" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]] || die "invalid --device-mac"
[[ "$HOST_MAC" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]] || die "invalid --host-mac"
[[ "$NAME" =~ ^[A-Za-z0-9._-]+$ ]] || die "--name contains unsafe characters"
[ -z "$FUNCTIONFS_MOUNT" ] || [[ "$FUNCTIONFS_MOUNT" = /* ]] || die "FunctionFS mount must be absolute"
[ -z "$MASS_STORAGE_IMAGE" ] || [[ "$MASS_STORAGE_IMAGE" = /* ]] || die "mass-storage image must be absolute"
[ "$BIND_EXISTING" -eq 0 ] || [ "$APPLY" -eq 1 ] || die "--bind-existing requires --apply"

case "$VID" in
    0x0456|0X0456)
        if [ "$APPLY" -eq 1 ] && [ "$ACK_OBSERVED_VID" -ne 1 ]; then
            die "applying Analog Devices VID 0x0456 requires --acknowledge-observed-vid"
        fi
        if [ "$APPLY" -eq 1 ] && [ "$SERIAL_SET" -ne 1 ] && [ "$BIND_EXISTING" -eq 0 ]; then
            die "--serial is required when applying the observed VID"
        fi
        ;;
esac

if [ -z "$SERIAL" ]; then
    SERIAL="P210TWIN-DRY-RUN-UNSET"
fi

GADGET="$CONFIGFS_ROOT/usb_gadget/$NAME"
CONFIG="$GADGET/configs/c.1"

print_cmd() {
    local item
    printf '  '
    for item in "$@"; do
        printf '%q ' "$item"
    done
    printf '\n'
}

do_cmd() {
    if [ "$APPLY" -eq 1 ]; then
        "$@"
    else
        print_cmd "$@"
    fi
}

write_attr() {
    local value=$1
    local path=$2
    if [ "$APPLY" -eq 1 ]; then
        printf '%s' "$value" >"$path"
    else
        printf '  printf %%s %q > %q\n' "$value" "$path"
    fi
}

link_function() {
    local source=$1
    local destination=$2
    if [ "$APPLY" -eq 1 ]; then
        [ -L "$destination" ] || ln -s "$source" "$destination"
    else
        print_cmd ln -s "$source" "$destination"
    fi
}

mounted_as() {
    local path=$1
    local type=$2
    awk -v wanted="$path" -v kind="$type" '$2 == wanted && $3 == kind { found=1 } END { exit(found ? 0 : 1) }' /proc/mounts
}

select_udc() {
    if [ -n "$UDC" ]; then
        return
    fi
    if [ "$APPLY" -eq 0 ]; then
        UDC="<auto-select-single-UDC>"
        return
    fi
    local -a candidates
    local path
    candidates=()
    for path in /sys/class/udc/*; do
        [ -e "$path" ] || continue
        candidates+=("${path##*/}")
    done
    if [ "${#candidates[@]}" -ne 1 ]; then
        die "expected exactly one UDC; pass --udc NAME (found ${#candidates[@]})"
    fi
    UDC=${candidates[0]}
}

if [ "$APPLY" -eq 1 ]; then
    [ "$(uname -s)" = "Linux" ] || die "--apply requires Linux"
    [ "$(id -u)" -eq 0 ] || die "--apply requires root"
    if command -v modprobe >/dev/null 2>&1; then
        modprobe libcomposite || die "cannot load libcomposite"
    fi
    mkdir -p "$CONFIGFS_ROOT"
    if ! mounted_as "$CONFIGFS_ROOT" configfs; then
        mount -t configfs none "$CONFIGFS_ROOT" || die "cannot mount configfs"
    fi
    [ -d "$CONFIGFS_ROOT/usb_gadget" ] || die "kernel exposes no usb_gadget configfs group"
    if [ -n "$MASS_STORAGE_IMAGE" ]; then
        [ -f "$MASS_STORAGE_IMAGE" ] && [ -r "$MASS_STORAGE_IMAGE" ] || die "mass-storage image is not a readable regular file"
    fi
fi

select_udc

printf 'Mode: %s\n' "$([ "$APPLY" -eq 1 ] && printf APPLY || printf DRY-RUN)"
printf 'Gadget: %s\n' "$GADGET"
printf 'UDC: %s\n' "$UDC"
printf 'Identity: %s:%s, bcdUSB %s, bcdDevice %s\n' "$VID" "$PID" "$BCD_USB" "$BCD_DEVICE"
printf 'Functions: RNDIS=%s ACM=%s mass-storage=%s FunctionFS-IIO=%s\n' \
    "$ENABLE_RNDIS" "$ENABLE_ACM" "$([ -n "$MASS_STORAGE_IMAGE" ] && printf yes || printf no)" \
    "$([ -n "$FUNCTIONFS_MOUNT" ] && printf yes || printf no)"
printf 'Warning: this is a transport skeleton, not a byte-exact descriptor or complete P210.\n'

if [ "$BIND_EXISTING" -eq 1 ]; then
    [ -d "$GADGET" ] || die "existing gadget not found: $GADGET"
    if [ -n "$FUNCTIONFS_MOUNT" ]; then
        READY="$GADGET/functions/ffs.iio/ready"
        [ -r "$READY" ] || die "existing gadget has no readable ffs.iio ready attribute"
        [ "$(tr -d '[:space:]' <"$READY")" = "1" ] || die "FunctionFS service is not ready; start it before binding"
        link_function "$GADGET/functions/ffs.iio" "$CONFIG/f4-iio"
    fi
    if [ -s "$GADGET/UDC" ]; then
        die "gadget is already bound to $(cat "$GADGET/UDC")"
    fi
    write_attr "$UDC" "$GADGET/UDC"
    printf 'Existing gadget bound. Capture its host-visible descriptors before use.\n'
    exit 0
fi

if [ "$APPLY" -eq 1 ] && [ -e "$GADGET" ]; then
    die "refusing to overwrite existing gadget: $GADGET"
fi

do_cmd mkdir "$GADGET"
write_attr "$VID" "$GADGET/idVendor"
write_attr "$PID" "$GADGET/idProduct"
write_attr "$BCD_USB" "$GADGET/bcdUSB"
write_attr "$BCD_DEVICE" "$GADGET/bcdDevice"

do_cmd mkdir -p "$GADGET/strings/0x409"
write_attr "$SERIAL" "$GADGET/strings/0x409/serialnumber"
write_attr "$MANUFACTURER" "$GADGET/strings/0x409/manufacturer"
write_attr "$PRODUCT" "$GADGET/strings/0x409/product"

do_cmd mkdir -p "$CONFIG/strings/0x409"
write_attr "P210 twin development composite" "$CONFIG/strings/0x409/configuration"
write_attr "0x80" "$CONFIG/bmAttributes"
write_attr "500" "$CONFIG/MaxPower"

FUNCTION_COUNT=0

if [ "$ENABLE_RNDIS" -eq 1 ]; then
    do_cmd mkdir "$GADGET/functions/rndis.usb0"
    write_attr "$DEVICE_MAC" "$GADGET/functions/rndis.usb0/dev_addr"
    write_attr "$HOST_MAC" "$GADGET/functions/rndis.usb0/host_addr"
    write_attr "1" "$GADGET/os_desc/use"
    write_attr "0xcd" "$GADGET/os_desc/b_vendor_code"
    write_attr "MSFT100" "$GADGET/os_desc/qw_sign"
    if [ "$APPLY" -eq 0 ]; then
        printf '  # If present, set RNDIS Microsoft compatible-ID files for this kernel.\n'
    elif [ -d "$GADGET/functions/rndis.usb0/os_desc/interface.rndis" ]; then
        write_attr "RNDIS" "$GADGET/functions/rndis.usb0/os_desc/interface.rndis/compatible_id"
        write_attr "5162001" "$GADGET/functions/rndis.usb0/os_desc/interface.rndis/sub_compatible_id"
    fi
    link_function "$GADGET/functions/rndis.usb0" "$CONFIG/f1-rndis"
    link_function "$CONFIG" "$GADGET/os_desc/c.1"
    FUNCTION_COUNT=$((FUNCTION_COUNT + 1))
fi

if [ -n "$MASS_STORAGE_IMAGE" ]; then
    do_cmd mkdir "$GADGET/functions/mass_storage.0"
    write_attr "1" "$GADGET/functions/mass_storage.0/lun.0/ro"
    write_attr "1" "$GADGET/functions/mass_storage.0/lun.0/removable"
    write_attr "1" "$GADGET/functions/mass_storage.0/lun.0/nofua"
    write_attr "$MASS_STORAGE_IMAGE" "$GADGET/functions/mass_storage.0/lun.0/file"
    link_function "$GADGET/functions/mass_storage.0" "$CONFIG/f2-mass-storage"
    FUNCTION_COUNT=$((FUNCTION_COUNT + 1))
fi

if [ "$ENABLE_ACM" -eq 1 ]; then
    do_cmd mkdir "$GADGET/functions/acm.GS0"
    link_function "$GADGET/functions/acm.GS0" "$CONFIG/f3-acm"
    FUNCTION_COUNT=$((FUNCTION_COUNT + 1))
fi

if [ -n "$FUNCTIONFS_MOUNT" ]; then
    do_cmd mkdir "$GADGET/functions/ffs.iio"
    do_cmd mkdir -p "$FUNCTIONFS_MOUNT"
    if [ "$APPLY" -eq 0 ]; then
        print_cmd mount -t functionfs iio "$FUNCTIONFS_MOUNT"
    elif ! mounted_as "$FUNCTIONFS_MOUNT" functionfs; then
        mount -t functionfs iio "$FUNCTIONFS_MOUNT" || die "cannot mount FunctionFS"
    fi
    FUNCTION_COUNT=$((FUNCTION_COUNT + 1))
    printf 'FunctionFS skeleton prepared but intentionally left unlinked and unbound.\n'
    printf 'Start the userspace IIO FunctionFS service, then use --apply --bind-existing.\n'
    exit 0
fi

[ "$FUNCTION_COUNT" -gt 0 ] || die "no USB function was selected"
write_attr "$UDC" "$GADGET/UDC"

if [ "$APPLY" -eq 1 ]; then
    printf 'Gadget bound. It is not descriptor-exact; capture and compare before relying on it.\n'
else
    printf 'Dry run complete. No configfs, mount, function, backing file, or UDC state changed.\n'
fi

