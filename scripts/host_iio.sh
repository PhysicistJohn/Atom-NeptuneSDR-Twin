#!/bin/sh
# Run the repo-local libiio v0.26 clients against the QEMU TCP forward.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CACHE=${HOST_LIBIIO_CACHE:-"$ROOT/.cache/host-libiio-v0.26"}
PREFIX=${HOST_LIBIIO_PREFIX:-"$CACHE/install"}
URI=${NEPTUNE_IIO_URI:-ip:127.0.0.1:30431}

usage() {
    cat <<'EOF'
Usage: host_iio.sh [--uri URI] [--prefix DIR] COMMAND [ARG ...]

Run the pinned, repo-local libiio v0.26 clients.  The default URI is the
QEMU user-network forward at ip:127.0.0.1:30431.

Commands:
  info [iio_info args]        Inspect the remote IIO context
  read [iio_readdev args]     Stream binary scan data to standard output
  version                     Print the pinned client/library version
  prefix                      Print the active installation prefix

Examples:
  scripts/host_iio.sh info
  scripts/host_iio.sh read -b 65536 -s 65536 cf-ad9361-lpc > rx.iq16le
  scripts/host_iio.sh --uri ip:192.168.2.1 info
EOF
}

die() {
    printf 'host_iio.sh: %s\n' "$*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --uri)
            [ "$#" -ge 2 ] || die "--uri requires a value"
            URI=$2
            shift 2
            ;;
        --prefix)
            [ "$#" -ge 2 ] || die "--prefix requires a directory"
            PREFIX=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            break
            ;;
        *)
            break
            ;;
    esac
done

command_name=${1:-info}
if [ "$#" -gt 0 ]; then
    shift
fi

case "$URI" in
    ''|-*) die "IIO URI must be non-empty and cannot begin with '-'" ;;
esac

run_tool() {
    tool=$1
    shift
    executable="$PREFIX/bin/$tool"
    [ -x "$executable" ] || \
        die "$tool is absent; run scripts/build_host_libiio.sh (looked in $PREFIX/bin)"
    case $(uname -s) in
        Darwin)
            DYLD_LIBRARY_PATH="$PREFIX/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
            export DYLD_LIBRARY_PATH
            exec "$executable" "$@"
            ;;
        Linux)
            LD_LIBRARY_PATH="$PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
            export LD_LIBRARY_PATH
            exec "$executable" "$@"
            ;;
        *)
            die "supported hosts are macOS and Linux"
            ;;
    esac
}

case "$command_name" in
    info)
        run_tool iio_info -u "$URI" "$@"
        ;;
    read)
        run_tool iio_readdev -u "$URI" "$@"
        ;;
    version)
        [ "$#" -eq 0 ] || die "version takes no arguments"
        run_tool iio_info -V
        ;;
    prefix)
        [ "$#" -eq 0 ] || die "prefix takes no arguments"
        printf '%s\n' "$PREFIX"
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
