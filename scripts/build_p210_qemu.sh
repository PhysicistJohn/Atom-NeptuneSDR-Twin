#!/bin/sh
# Build the native QEMU 10.0.2 P210 machine and custom RF/PL devices.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$ROOT/scripts/cache_relocation.sh"
CACHE=${P210_QEMU_CACHE:-"$ROOT/.cache/qemu-p210"}
TOOLS="$CACHE/tools"
DOWNLOADS="$CACHE/downloads"
ENV="$CACHE/env"
SOURCE="$CACHE/src/qemu-10.0.2"
BUILD="$SOURCE/build-p210"
OUTPUT="$CACHE/bin/qemu-system-arm"
OUTPUT_LINK="../src/qemu-10.0.2/build-p210/qemu-system-arm"

QEMU_VERSION=10.0.2
QEMU_ARCHIVE="qemu-${QEMU_VERSION}.tar.xz"
QEMU_URL="https://download.qemu.org/${QEMU_ARCHIVE}"
QEMU_SHA256=ef786f2398cb5184600f69aef4d5d691efd44576a3cff4126d38d4c6fec87759
MICROMAMBA_VERSION=2.8.1-0
MICROMAMBA_SHA256=de71a646b73af92dd663e6ddc78993a6a4d47ea28b5d8908c3cc2b9c3077e528
MICROMAMBA_URL="https://github.com/mamba-org/micromamba-releases/releases/download/${MICROMAMBA_VERSION}/micromamba-osx-arm64"
MICROMAMBA="$TOOLS/micromamba"
PATCH="$ROOT/qemu/patches/0001-p210-zynq-devices.patch"
DEVICE_TREE="$ROOT/cosim/qemu-10.0.2"
MODE=build

case ${1:-} in
    '') ;;
    --verify) MODE=verify ;;
    -h|--help)
        printf '%s\n' 'Usage: build_p210_qemu.sh [--verify]'
        printf '%s\n' 'Build, or verify without rebuilding, the pinned P210 QEMU.'
        exit 0
        ;;
    *) printf '%s\n' 'Usage: build_p210_qemu.sh [--verify]' >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || { printf '%s\n' 'Usage: build_p210_qemu.sh [--verify]' >&2; exit 2; }

if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
    printf '%s\n' \
        "build_p210_qemu.sh: the pinned native toolchain currently targets macOS arm64" >&2
    exit 2
fi

sha256_file() {
    shasum -a 256 "$1" | awk '{print $1}'
}

download_locked() {
    url=$1
    destination=$2
    expected=$3
    if [ -f "$destination" ]; then
        actual=$(sha256_file "$destination")
        if [ "$actual" != "$expected" ]; then
            printf 'build_p210_qemu.sh: hash mismatch for %s\n' "$destination" >&2
            exit 1
        fi
        return
    fi
    temporary="${destination}.part"
    rm -f "$temporary"
    curl -fL --retry 3 --output "$temporary" "$url"
    actual=$(sha256_file "$temporary")
    if [ "$actual" != "$expected" ]; then
        rm -f "$temporary"
        printf 'build_p210_qemu.sh: downloaded hash mismatch for %s\n' "$url" >&2
        exit 1
    fi
    mv "$temporary" "$destination"
}

verify_binary() {
    [ -x "$OUTPUT" ] || return 1
    [ -L "$OUTPUT" ] || return 1
    [ "$(readlink "$OUTPUT")" = "$OUTPUT_LINK" ] || return 1
    "$OUTPUT" --version 2>/dev/null | grep -q 'QEMU emulator version 10\.0\.2' || return 1
    "$OUTPUT" -machine xilinx-zynq-a9,help 2>&1 |
        grep -q 'p210=<bool>.*Enable HAMGEEK P210 SDR devices' || return 1
}

guard_relocatable_cache "$CACHE" qemu-p210-v1 build_p210_qemu.sh \
    env mamba-root src/qemu-10.0.2/build-p210 bin/qemu-system-arm

if [ "$MODE" = verify ]; then
    if [ "$CACHE_RELOCATION_ACTION" = invalidated ]; then
        printf '%s\n' \
            'build_p210_qemu.sh: the cache moved or predated relocation tracking; rebuild without --verify' >&2
        exit 1
    fi
    if ! verify_binary; then
        printf '%s\n' \
            'build_p210_qemu.sh: cached P210 QEMU failed verification; rebuild without --verify' >&2
        exit 1
    fi
    printf 'qemu=%s\n' "$OUTPUT"
    printf '%s\n' 'cache=verified'
    exit 0
fi

mkdir -p "$TOOLS" "$DOWNLOADS" "$CACHE/bin"
download_locked "$MICROMAMBA_URL" "$MICROMAMBA" "$MICROMAMBA_SHA256"
chmod 755 "$MICROMAMBA"

if [ ! -x "$ENV/bin/python3" ]; then
    MAMBA_ROOT_PREFIX="$CACHE/mamba-root" "$MICROMAMBA" create -y \
        -p "$ENV" -c conda-forge --strict-channel-priority \
        python=3.12.13 meson=1.11.2 ninja=1.13.2 pkg-config=0.29.2 \
        glib=2.88.2 pixman=0.46.4 dtc=1.7.2 libslirp=4.4.0 make=4.4.1 \
        distlib=0.4.3
elif ! "$ENV/bin/python3" -c 'import distlib' >/dev/null 2>&1; then
    MAMBA_ROOT_PREFIX="$CACHE/mamba-root" "$MICROMAMBA" install -y \
        -p "$ENV" -c conda-forge --strict-channel-priority distlib=0.4.3
fi

ARCHIVE="$DOWNLOADS/$QEMU_ARCHIVE"
download_locked "$QEMU_URL" "$ARCHIVE" "$QEMU_SHA256"
extract_source() {
    rm -rf "$SOURCE"
    mkdir -p "$CACHE/src"
    tar -C "$CACHE/src" -xf "$ARCHIVE"
}

copy_if_changed() {
    source_file=$1
    destination_file=$2
    if [ ! -f "$destination_file" ] || ! cmp -s "$source_file" "$destination_file"; then
        cp "$source_file" "$destination_file"
    fi
}
if [ ! -f "$SOURCE/VERSION" ]; then
    extract_source
fi

PATCH_SHA256=$(sha256_file "$PATCH")
PATCH_STAMP="$SOURCE/.p210-integration.sha256"
arm_marker=$(grep -c 'hw/misc/p210_sdr.h' "$SOURCE/hw/arm/xilinx_zynq.c" || true)
misc_marker=$(grep -c "files('p210_sdr.c')" "$SOURCE/hw/misc/meson.build" || true)
ssi_marker=$(grep -c "files('p210_ad9361.c')" "$SOURCE/hw/ssi/meson.build" || true)
if [ "$arm_marker" -gt 0 ] && [ "$misc_marker" -gt 0 ] && [ "$ssi_marker" -gt 0 ]; then
    stamped=
    if [ -f "$PATCH_STAMP" ]; then
        stamped=$(sed -n '1p' "$PATCH_STAMP")
    fi
    if [ "$stamped" != "$PATCH_SHA256" ]; then
        extract_source
        arm_marker=0
        misc_marker=0
        ssi_marker=0
    fi
fi
if [ "$arm_marker" -gt 0 ] && [ "$misc_marker" -gt 0 ] && [ "$ssi_marker" -gt 0 ]; then
    :
elif [ "$arm_marker" -gt 0 ] || [ "$misc_marker" -gt 0 ] || [ "$ssi_marker" -gt 0 ]; then
    printf '%s\n' 'build_p210_qemu.sh: QEMU source has a partial P210 integration' >&2
    exit 1
else
    patch --batch --forward -d "$SOURCE" -p1 <"$PATCH"
fi
printf '%s\n' "$PATCH_SHA256" >"$PATCH_STAMP"

# Device sources live in the repository so editing them forces the next Ninja
# invocation to rebuild the corresponding objects without re-extracting QEMU.
copy_if_changed "$DEVICE_TREE/hw/misc/p210_sdr.c" "$SOURCE/hw/misc/p210_sdr.c"
copy_if_changed "$DEVICE_TREE/hw/misc/p210_fft.c" "$SOURCE/hw/misc/p210_fft.c"
copy_if_changed "$DEVICE_TREE/hw/ssi/p210_ad9361.c" "$SOURCE/hw/ssi/p210_ad9361.c"
copy_if_changed "$DEVICE_TREE/include/hw/misc/p210_sdr.h" "$SOURCE/include/hw/misc/p210_sdr.h"
copy_if_changed "$DEVICE_TREE/include/hw/misc/p210_fft.h" "$SOURCE/include/hw/misc/p210_fft.h"
copy_if_changed "$DEVICE_TREE/hw/misc/p210_operator.c" "$SOURCE/hw/misc/p210_operator.c"
copy_if_changed "$DEVICE_TREE/hw/misc/p210_twiddle_rom.h" "$SOURCE/hw/misc/p210_twiddle_rom.h"
copy_if_changed "$DEVICE_TREE/include/hw/misc/p210_operator.h" "$SOURCE/include/hw/misc/p210_operator.h"

# Wire the v2 operator device into the P210 machine, idempotently (the base
# 0001 patch predates it). Adds the meson build entry, the machine include, and
# the sysbus instantiation at 0x7c460000 / GIC SPI 59 next to the v1 FFT.
python3 - "$SOURCE" <<'WIRE'
import sys, io
src = sys.argv[1]
meson = src + "/hw/misc/meson.build"
m = open(meson).read()
if "p210_operator.c" not in m:
    m = m.replace("system_ss.add(when: 'CONFIG_ZYNQ', if_true: files('p210_fft.c'))",
                  "system_ss.add(when: 'CONFIG_ZYNQ', if_true: files('p210_fft.c'))\n"
                  "system_ss.add(when: 'CONFIG_ZYNQ', if_true: files('p210_operator.c'))")
    open(meson, "w").write(m)
mach = src + "/hw/arm/xilinx_zynq.c"
lines = open(mach).read().split("\n")
text = "\n".join(lines)
if "p210_operator.h" not in text:
    for i, l in enumerate(lines):
        if '#include "hw/misc/p210_fft.h"' in l:
            lines.insert(i + 1, '#include "hw/misc/p210_operator.h"'); break
if "TYPE_P210_OPERATOR" not in text:
    for i, l in enumerate(lines):
        if "sysbus_connect_irq(busdev, 0, pic[58]);" in l:
            lines[i+1:i+1] = [
                "",
                "        dev = qdev_new(TYPE_P210_OPERATOR);",
                "        busdev = SYS_BUS_DEVICE(dev);",
                "        sysbus_realize_and_unref(busdev, &error_fatal);",
                "        sysbus_mmio_map(busdev, 0, 0x7c460000);",
                "        sysbus_connect_irq(busdev, 0, pic[59]);",
            ]
            break
open(mach, "w").write("\n".join(lines))
WIRE

export PATH="$ENV/bin:$PATH"
export PKG_CONFIG_PATH="$ENV/lib/pkgconfig:$ENV/share/pkgconfig"
export CFLAGS="-I$ENV/include"
export LDFLAGS="-L$ENV/lib -Wl,-rpath,$ENV/lib"

if [ ! -f "$BUILD/build.ninja" ]; then
    mkdir -p "$BUILD"
    (
        cd "$BUILD"
        ../configure \
            --target-list=arm-softmmu \
            --enable-fdt=system \
            --enable-pixman \
            --enable-slirp \
            --disable-docs \
            --disable-tools \
            --disable-guest-agent \
            --disable-cocoa \
            --disable-gtk \
            --disable-sdl \
            --disable-vnc \
            --disable-curl \
            --disable-gnutls \
            --disable-libssh \
            --disable-werror \
            --audio-drv-list=
    )
fi

ninja -C "$BUILD" qemu-system-arm
# The public entry point must survive moving the repository and its .cache as
# one directory tree.  Meson and conda state are guarded above because they do
# embed absolute prefixes; this link does not.
ln -sfn "$OUTPUT_LINK" "$OUTPUT"

if ! verify_binary; then
    printf '%s\n' 'build_p210_qemu.sh: built binary failed P210 machine verification' >&2
    exit 1
fi

printf 'qemu=%s\n' "$OUTPUT"
printf 'version=%s\n' "$QEMU_VERSION"
printf 'machine=xilinx-zynq-a9,p210=on\n'
printf 'source_sha256=%s\n' "$QEMU_SHA256"
