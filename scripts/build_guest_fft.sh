#!/bin/sh
# Build the static ARM guest FFT/IIO/TCP firmware component reproducibly.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ZIG=${ZIG:-"$ROOT/.cache/fft-toolchain/bin/zig"}
OUTPUT=${P210_GUEST_OUTPUT:-"$ROOT/.cache/p210-guest/neptune-fft-streamer"}
EXPECTED_ZIG=0.14.1

if [ ! -x "$ZIG" ]; then
    printf '%s\n' "build_guest_fft.sh: Zig $EXPECTED_ZIG is required at $ZIG" >&2
    printf '%s\n' "Set ZIG to a pinned Zig $EXPECTED_ZIG executable." >&2
    exit 2
fi

actual=$($ZIG version)
if [ "$actual" != "$EXPECTED_ZIG" ]; then
    printf '%s\n' "build_guest_fft.sh: expected Zig $EXPECTED_ZIG, found $actual" >&2
    exit 2
fi

mkdir -p "$(dirname -- "$OUTPUT")"
"$ZIG" cc \
    -target arm-linux-musleabihf \
    -mcpu=cortex_a9 \
    -O2 -static -s \
    -Wall -Wextra -Werror \
    "$ROOT/firmware/neptune_fft_streamer.c" \
    -lm -o "$OUTPUT"

sha=$(shasum -a 256 "$OUTPUT" | awk '{print $1}')
printf 'guest_fft=%s\n' "$OUTPUT"
printf 'sha256=%s\n' "$sha"
printf 'toolchain=zig-%s target=arm-linux-musleabihf cpu=cortex_a9 static=yes\n' "$actual"
