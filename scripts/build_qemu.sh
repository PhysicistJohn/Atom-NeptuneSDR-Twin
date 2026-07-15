#!/bin/sh
# Provision the official UTM QEMU runtime in this repository's ignored cache.
#
# UTM ships a universal QEMU 10.0.2 build with all macOS framework
# dependencies.  Its QEMU target is a dylib, so the small QEMULauncher is
# copied and ad-hoc re-signed without UTM's inherited sandbox entitlement.
# The QEMU dylib itself remains the verified upstream binary.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CACHE=${QEMU_RUNTIME_CACHE:-"$ROOT/.cache/qemu-runtime"}
UTM_VERSION=4.7.5
UTM_DMG_SHA256=a8435c93cfb5f8bbfeea4b134cfad1ac66b67632b75e438c63b1a8ae043bef0e
UTM_URL="https://github.com/utmapp/UTM/releases/download/v${UTM_VERSION}/UTM.dmg"
DMG="$CACHE/downloads/UTM-v${UTM_VERSION}.dmg"
RUNTIME="$CACHE/utm-${UTM_VERSION}/UTM.app"
MOUNT="$CACHE/mount-${UTM_VERSION}"
WRAPPER="$ROOT/scripts/qemu-system-arm-local.sh"

if [ "$(uname -s)" != Darwin ]; then
    echo "error: this repo-local provider is for macOS; install qemu-system-arm on this host" >&2
    exit 2
fi

actual_sha256() {
    shasum -a 256 "$1" | awk '{print $1}'
}

verify_runtime() {
    [ -x "$WRAPPER" ] || return 1
    [ -x "$RUNTIME/Contents/XPCServices/QEMUHelper.xpc/Contents/MacOS/QEMULauncher.app/Contents/MacOS/QEMULauncher" ] || return 1
    [ -f "$RUNTIME/Contents/Frameworks/qemu-arm-softmmu.framework/Versions/A/qemu-arm-softmmu" ] || return 1
    "$WRAPPER" --version 2>/dev/null | grep -q 'QEMU emulator version 10\.0\.2' || return 1
    "$WRAPPER" -machine help 2>/dev/null | grep -q '^xilinx-zynq-a9 ' || return 1
}

if verify_runtime; then
    echo "QEMU 10.0.2 runtime ready: $WRAPPER"
    exit 0
fi

mkdir -p "$CACHE/downloads" "$MOUNT" "$(dirname "$RUNTIME")"
if [ -f "$DMG" ] && [ "$(actual_sha256 "$DMG")" != "$UTM_DMG_SHA256" ]; then
    echo "error: cached UTM image hash mismatch: $DMG" >&2
    exit 1
fi
if [ ! -f "$DMG" ]; then
    temporary="$DMG.part"
    curl -fL --retry 3 --output "$temporary" "$UTM_URL"
    if [ "$(actual_sha256 "$temporary")" != "$UTM_DMG_SHA256" ]; then
        echo "error: downloaded UTM image hash mismatch" >&2
        exit 1
    fi
    mv "$temporary" "$DMG"
fi

hdiutil verify "$DMG" >/dev/null

detach_mount() {
    if mount | grep -Fq " on $MOUNT "; then
        hdiutil detach "$MOUNT" >/dev/null || true
    fi
}
trap detach_mount EXIT HUP INT TERM
detach_mount
hdiutil attach -nobrowse -readonly -mountpoint "$MOUNT" "$DMG" >/dev/null
SOURCE="$MOUNT/UTM.app"

# Verify Apple's code signature and notarization before extracting anything.
codesign --verify --deep --strict "$SOURCE"
spctl --assess --type execute "$SOURCE"

STAGING="$CACHE/utm-${UTM_VERSION}.staging/UTM.app"
if [ -e "$STAGING" ]; then
    echo "error: stale staging runtime exists: $STAGING" >&2
    exit 1
fi
mkdir -p "$STAGING/Contents/Frameworks"

# QEMU's direct dependency closure is small, but copying every non-QEMU
# framework makes optional machine facilities deterministic.  Other CPU
# target frameworks are intentionally omitted.
for framework in "$SOURCE"/Contents/Frameworks/*.framework; do
    name=$(basename "$framework")
    case "$name" in
        qemu-arm-softmmu.framework)
            ditto "$framework" "$STAGING/Contents/Frameworks/$name"
            ;;
        qemu-*.framework|qemu-img.framework)
            ;;
        *)
            ditto "$framework" "$STAGING/Contents/Frameworks/$name"
            ;;
    esac
done
if [ -f "$SOURCE/Contents/Frameworks/libswift_Concurrency.dylib" ]; then
    ditto "$SOURCE/Contents/Frameworks/libswift_Concurrency.dylib" \
        "$STAGING/Contents/Frameworks/libswift_Concurrency.dylib"
fi

launcher_relative=Contents/XPCServices/QEMUHelper.xpc/Contents/MacOS/QEMULauncher.app/Contents/MacOS/QEMULauncher
mkdir -p "$STAGING/$(dirname "$launcher_relative")"
ditto "$SOURCE/$launcher_relative" "$STAGING/$launcher_relative"

ENTITLEMENTS="$ROOT/scripts/qemu-launcher-entitlements.plist"

# Direct invocation is outside UTM's app sandbox.  Keeping the inherited
# sandbox entitlement makes macOS terminate the launcher, hence this local
# ad-hoc signature retains only the JIT entitlement required by TCG.
codesign --force --sign - --entitlements "$ENTITLEMENTS" "$STAGING/$launcher_relative" >/dev/null

if [ -e "$RUNTIME" ]; then
    echo "error: incomplete runtime exists: $RUNTIME" >&2
    exit 1
fi
mv "$STAGING" "$RUNTIME"
rmdir "$(dirname "$STAGING")"

if ! verify_runtime; then
    echo "error: provisioned QEMU cannot provide xilinx-zynq-a9" >&2
    exit 1
fi
echo "QEMU 10.0.2 runtime ready: $WRAPPER"
