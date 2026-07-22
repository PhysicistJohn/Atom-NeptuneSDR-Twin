#!/usr/bin/env bash
set -euo pipefail

ATOM_NEPTUNE_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PYTHONPATH="$ATOM_NEPTUNE_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

# The ordinary source gate must not accidentally execute an unrelated or stale
# optional QEMU build from .cache. Integration runs set these paths explicitly;
# here an empty private directory makes those tests report a deliberate skip.
ATOM_NEPTUNE_NO_QEMU=$(mktemp -d "${TMPDIR:-/tmp}/atom-neptune-source-gate.XXXXXX")
trap 'rmdir "$ATOM_NEPTUNE_NO_QEMU"' EXIT HUP INT TERM
export P210_QEMU_BUILD_DIR="$ATOM_NEPTUNE_NO_QEMU"
export P210_QEMU_BINARY="$ATOM_NEPTUNE_NO_QEMU/qemu-system-arm"

cd "$ATOM_NEPTUNE_ROOT"
python3 -m compileall -q src scripts tests cosim/tests
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s cosim/tests -v

for script in scripts/*.sh; do
  bash -n "$script"
done

python3 -m neptunesdr_twin contracts >/dev/null
python3 -m neptunesdr_twin fft-plan >/dev/null
python3 -m neptunesdr_twin serve --dry-run >/dev/null
python3 -m neptunesdr_twin usbip-serve --dry-run >/dev/null
python3 -m neptunesdr_twin appliance --dry-run >/dev/null
./scripts/linux_usb_gadget.sh >/dev/null
git diff --check
