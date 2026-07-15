#!/bin/sh
# Build the exact libiio release used by the Pluto v0.39 userspace.
#
# Everything is cloned, built, and installed below the repository's ignored
# cache.  This script never invokes a package manager and never installs into
# /usr, /usr/local, or /Library.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CACHE=${HOST_LIBIIO_CACHE:-"$ROOT/.cache/host-libiio-v0.26"}
SOURCE="$CACHE/source"
BUILD="$CACHE/build"
PREFIX=${HOST_LIBIIO_PREFIX:-"$CACHE/install"}

LIBIIO_REPOSITORY=https://github.com/analogdevicesinc/libiio.git
LIBIIO_TAG=v0.26
LIBIIO_COMMIT=a0eca0d2bf10326506fb762f0eec14255b27bef5
LIBIIO_TREE=d35513bc71252029f769a85a021ba8a858560246

usage() {
    cat <<'EOF'
Usage: build_host_libiio.sh [--verify | --print-prefix]

With no option, clone, verify, build, install, and execute the version check
for the pinned libiio v0.26 host tools.  Output stays in the repository cache.

Options:
  --verify        Verify an existing source tree and installed tools only
  --print-prefix  Print the repo-local installation prefix and exit
  -h, --help      Show this help

Environment:
  HOST_LIBIIO_CACHE   Override the source/build/cache root
  HOST_LIBIIO_PREFIX  Override the installation prefix
  HOST_LIBIIO_JOBS    Native build parallelism (default: detected CPU count)
  CMAKE               Explicit cmake executable
EOF
}

die() {
    printf 'build_host_libiio.sh: %s\n' "$*" >&2
    exit 1
}

mode=build
case ${1:-} in
    '') ;;
    --verify) mode=verify ;;
    --print-prefix) printf '%s\n' "$PREFIX"; exit 0 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || { usage >&2; exit 2; }

case "$PREFIX" in
    /|/System|/System/*|/Library|/Library/*|/usr|/usr/*|/opt|/opt/*)
        die "refusing a system installation prefix: $PREFIX"
        ;;
esac

case $(uname -s) in
    Darwin)
        [ "$(uname -m)" = arm64 ] || die "the macOS build is locked to Apple Silicon (arm64)"
        host_os=darwin
        ;;
    Linux)
        host_os=linux
        ;;
    *)
        die "supported hosts are macOS arm64 and Linux"
        ;;
esac

find_cmake() {
    if [ -n "${CMAKE:-}" ]; then
        [ -x "$CMAKE" ] || die "CMAKE is not executable: $CMAKE"
        printf '%s\n' "$CMAKE"
    elif command -v cmake >/dev/null 2>&1; then
        command -v cmake
    elif [ -x "$ROOT/.venv/bin/cmake" ]; then
        printf '%s\n' "$ROOT/.venv/bin/cmake"
    else
        die "cmake >= 3.10 is required (the repo .venv is also searched)"
    fi
}

verify_source() {
    [ -d "$SOURCE/.git" ] || die "pinned source is absent: $SOURCE"
    actual_commit=$(git -C "$SOURCE" rev-parse HEAD 2>/dev/null) || die "cannot read source commit"
    [ "$actual_commit" = "$LIBIIO_COMMIT" ] || \
        die "source commit mismatch: expected $LIBIIO_COMMIT, got $actual_commit"
    actual_tree=$(git -C "$SOURCE" rev-parse 'HEAD^{tree}' 2>/dev/null) || die "cannot read source tree"
    [ "$actual_tree" = "$LIBIIO_TREE" ] || \
        die "source tree mismatch: expected $LIBIIO_TREE, got $actual_tree"
    [ -z "$(git -C "$SOURCE" status --porcelain --untracked-files=no)" ] || \
        die "tracked files in the pinned source tree were modified"
}

run_installed() {
    tool=$1
    shift
    case "$host_os" in
        darwin)
            DYLD_LIBRARY_PATH="$PREFIX/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}" \
                "$PREFIX/bin/$tool" "$@"
            ;;
        linux)
            LD_LIBRARY_PATH="$PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
                "$PREFIX/bin/$tool" "$@"
            ;;
    esac
}

verify_install() {
    [ -x "$PREFIX/bin/iio_info" ] || die "installed iio_info is absent: $PREFIX/bin/iio_info"
    [ -x "$PREFIX/bin/iio_readdev" ] || die "installed iio_readdev is absent: $PREFIX/bin/iio_readdev"
    version=$(run_installed iio_info -V 2>&1) || die "installed iio_info cannot execute"
    printf '%s\n' "$version" | grep -Fq 'iio_info version: 0.26' || \
        die "installed iio_info does not report version 0.26"
    printf '%s\n' "$version" | grep -Fq 'git tag:a0eca0d' || \
        die "installed iio_info does not report pinned commit a0eca0d"
    printf '%s\n' "$version" | grep -Eq 'backends:.*(^|[[:space:]])xml([[:space:]]|$)' || \
        die "installed libiio lacks the XML backend"
    printf '%s\n' "$version" | grep -Eq 'backends:.*(^|[[:space:]])ip([[:space:]]|$)' || \
        die "installed libiio lacks the network backend"
    printf '%s\n' "$version"
}

if [ "$mode" = verify ]; then
    command -v git >/dev/null 2>&1 || die "git is required to verify the source"
    verify_source
    verify_install
    exit 0
fi

command -v git >/dev/null 2>&1 || die "git is required"
command -v cc >/dev/null 2>&1 || die "a native C compiler is required"
CMAKE_BIN=$(find_cmake)

mkdir -p "$CACHE"
if [ ! -e "$SOURCE" ]; then
    git clone --branch "$LIBIIO_TAG" --depth 1 "$LIBIIO_REPOSITORY" "$SOURCE"
fi
verify_source

mkdir -p "$BUILD" "$PREFIX"

configure() {
    "$CMAKE_BIN" "$SOURCE" "$@" \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="$PREFIX" \
        -DBUILD_SHARED_LIBS=ON \
        -DOSX_FRAMEWORK=OFF \
        -DOSX_PACKAGE=OFF \
        -DWITH_NETWORK_BACKEND=ON \
        -DHAVE_DNS_SD=OFF \
        -DWITH_USB_BACKEND=OFF \
        -DWITH_SERIAL_BACKEND=OFF \
        -DWITH_ZSTD=OFF \
        -DWITH_XML_BACKEND=ON \
        -DWITH_TESTS=ON \
        -DWITH_EXAMPLES=OFF \
        -DWITH_DOC=OFF \
        -DWITH_MAN=OFF \
        -DCPP_BINDINGS=OFF \
        -DCSHARP_BINDINGS=OFF \
        -DPYTHON_BINDINGS=OFF \
        -DWITH_LOCAL_BACKEND=OFF \
        -DWITH_IIOD=OFF
}

if [ ! -f "$BUILD/CMakeCache.txt" ]; then
    if command -v ninja >/dev/null 2>&1; then
        (cd "$BUILD" && configure -G Ninja)
    elif [ -x "$ROOT/.venv/bin/ninja" ]; then
        (cd "$BUILD" && configure -G Ninja -DCMAKE_MAKE_PROGRAM="$ROOT/.venv/bin/ninja")
    else
        (cd "$BUILD" && configure -G 'Unix Makefiles')
    fi
else
    (cd "$BUILD" && configure)
fi

if [ -n "${HOST_LIBIIO_JOBS:-}" ]; then
    jobs=$HOST_LIBIIO_JOBS
elif [ "$host_os" = darwin ]; then
    jobs=$(sysctl -n hw.logicalcpu 2>/dev/null || printf '1')
else
    jobs=$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1')
fi
case "$jobs" in
    ''|*[!0-9]*) die "HOST_LIBIIO_JOBS must be a positive integer" ;;
esac
[ "$jobs" -gt 0 ] || die "HOST_LIBIIO_JOBS must be a positive integer"

CMAKE_BUILD_PARALLEL_LEVEL=$jobs "$CMAKE_BIN" --build "$BUILD" --target install
verify_install
printf 'libiio host tools ready: %s\n' "$PREFIX"
