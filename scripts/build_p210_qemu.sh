#!/bin/sh
# Build the native QEMU 10.0.2 P210 machine and custom RF/PL devices.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CACHE=${P210_QEMU_CACHE:-"$ROOT/.cache/qemu-p210"}
TOOLS="$CACHE/tools"
DOWNLOADS="$CACHE/downloads"
ENV="$CACHE/env"
SOURCE="$CACHE/src/qemu-10.0.2"
BUILD="$SOURCE/build-p210"
OUTPUT="$CACHE/bin/qemu-system-arm"

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
    "$OUTPUT" --version 2>/dev/null | grep -q 'QEMU emulator version 10\.0\.2' || return 1
    "$OUTPUT" -machine xilinx-zynq-a9,help 2>&1 |
        grep -q 'p210=<bool>.*Enable HAMGEEK P210 SDR devices' || return 1
}

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
cp "$DEVICE_TREE/hw/misc/p210_sdr.c" "$SOURCE/hw/misc/p210_sdr.c"
cp "$DEVICE_TREE/hw/misc/p210_fft.c" "$SOURCE/hw/misc/p210_fft.c"
cp "$DEVICE_TREE/hw/ssi/p210_ad9361.c" "$SOURCE/hw/ssi/p210_ad9361.c"
cp "$DEVICE_TREE/include/hw/misc/p210_sdr.h" "$SOURCE/include/hw/misc/p210_sdr.h"
cp "$DEVICE_TREE/include/hw/misc/p210_fft.h" "$SOURCE/include/hw/misc/p210_fft.h"

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
ln -sfn "$BUILD/qemu-system-arm" "$OUTPUT"

if ! verify_binary; then
    printf '%s\n' 'build_p210_qemu.sh: built binary failed P210 machine verification' >&2
    exit 1
fi

printf 'qemu=%s\n' "$OUTPUT"
printf 'version=%s\n' "$QEMU_VERSION"
printf 'machine=xilinx-zynq-a9,p210=on\n'
printf 'source_sha256=%s\n' "$QEMU_SHA256"
