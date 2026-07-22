#!/bin/sh
# Reproduce the complete pre-arrival acceptance matrix and retain machine logs.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUTPUT=${P210_ACCEPTANCE_OUTPUT:-"$ROOT/.cache/acceptance"}
BUILD=yes
FIRMWARE=yes

usage() {
    cat <<'EOF'
Usage: accept_virtual_twin.sh [OPTIONS]

Run reference, protocol, source/co-simulation, locked-artifact and full ARM
firmware acceptance. The default may build QEMU/toolchains and download only
hash-locked public inputs; it never flashes hardware.

Options:
  --no-build          Reuse an existing P210 QEMU/ARM/host-libiio build
  --source-only       Run the source gate with exactly 15 explicit QEMU skips
  --reference-only    Deprecated alias for --source-only
  -h, --help          Show this help
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-build) BUILD=no; shift ;;
        --source-only|--reference-only) FIRMWARE=no; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

FIRMWAVE_ROOT=$(python3 "$ROOT/scripts/resolve_firmwave.py")
export NEPTUNESDR_FIRMWAVE_ROOT="$FIRMWAVE_ROOT"
FIRMWARE_CACHE=${P210_FIRMWARE_CACHE:-"$ROOT/.cache/firmwave/firmware"}
RUN_ID=$(date -u '+%Y%m%dT%H%M%SZ')-$$
OUTPUT_BASE=$OUTPUT
OUTPUT="$OUTPUT_BASE/runs/$RUN_ID"
python3 "$ROOT/scripts/acceptance_gate.py" start \
    --root "$ROOT" --firmwave-root "$FIRMWAVE_ROOT" \
    --output "$OUTPUT" --run-id "$RUN_ID" \
    --mode "$(if [ "$FIRMWARE" = yes ]; then printf full; else printf source; fi)"
printf '%s\n' RUNNING >"$OUTPUT/status"
acceptance_done=no
record_failure() {
    result=$?
    if [ "$acceptance_done" != yes ]; then
        printf 'FAILED exit=%s\n' "$result" >"$OUTPUT/status"
    fi
}
trap record_failure EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
export PYTHONPATH="$FIRMWAVE_ROOT/src:$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

# The live qtests discover the pinned executable. Build it before the cosim
# suite on a fresh checkout so they cannot be silently skipped in a full run.
if [ "$FIRMWARE" = yes ]; then
    QEMU_CACHE=${P210_QEMU_CACHE:-"$ROOT/.cache/qemu-p210"}
    QEMU_BINARY=${P210_QEMU_BINARY:-"$QEMU_CACHE/bin/qemu-system-arm"}
    QEMU_BUILD_DIR=${P210_QEMU_BUILD_DIR:-"$QEMU_CACHE/src/qemu-10.0.2/build-p210"}
    GUEST=${P210_GUEST_OUTPUT:-"$ROOT/.cache/p210-guest/neptune-fft-streamer"}
    BUILD_IDENTITY=${P210_BUILD_IDENTITY:-"$QEMU_CACHE/p210-runtime-build-identity.json"}
    LIBIIO_PREFIX=$("$ROOT/scripts/build_host_libiio.sh" --print-prefix)
    HOST_IIO_INFO="$LIBIIO_PREFIX/bin/iio_info"
    HOST_IIO_READDEV="$LIBIIO_PREFIX/bin/iio_readdev"
    case $(uname -s) in
        Darwin) HOST_LIBIIO="$LIBIIO_PREFIX/lib/libiio.dylib" ;;
        Linux) HOST_LIBIIO="$LIBIIO_PREFIX/lib/libiio.so" ;;
        *) HOST_LIBIIO="$LIBIIO_PREFIX/lib/libiio.unsupported-host" ;;
    esac
    if [ "$BUILD" = yes ]; then
        "$ROOT/scripts/build_p210_qemu.sh" \
            >"$OUTPUT/qemu-build.log" 2>&1
    else
        # Hash and source-identity verification intentionally precedes every
        # command that would execute a cached QEMU or host-libiio binary.
        python3 "$ROOT/scripts/acceptance_gate.py" verify-build-cache \
            --root "$ROOT" \
            --firmwave-root "$FIRMWAVE_ROOT" \
            --qemu "$QEMU_BINARY" \
            --guest "$GUEST" \
            --iio-info "$HOST_IIO_INFO" \
            --iio-readdev "$HOST_IIO_READDEV" \
            --libiio "$HOST_LIBIIO" \
            --output "$BUILD_IDENTITY" \
            >"$OUTPUT/build-cache-verify.log" 2>&1
        "$ROOT/scripts/build_p210_qemu.sh" --verify \
            >>"$OUTPUT/build-cache-verify.log" 2>&1
        "$ROOT/scripts/build_host_libiio.sh" --verify \
            >>"$OUTPUT/build-cache-verify.log" 2>&1
    fi

    test -x "$QEMU_BINARY"
    test -f "$QEMU_BUILD_DIR/compile_commands.json"
    python3 - "$QEMU_BINARY" "$QEMU_CACHE/bin/qemu-system-arm" \
        "$QEMU_BUILD_DIR/qemu-system-arm" <<'PY'
import os
import sys
if not os.path.samefile(sys.argv[1], sys.argv[2]) or not os.path.samefile(sys.argv[1], sys.argv[3]):
    raise SystemExit("configured, cached, and build-directory QEMU binaries are not identical")
PY
    "$QEMU_BINARY" --version | grep -Fq 'QEMU emulator version 10.0.2'
    "$QEMU_BINARY" -machine xilinx-zynq-a9,help 2>&1 |
        grep -q 'p210=<bool>.*Enable HAMGEEK P210 SDR devices'
    export P210_QEMU_BINARY="$QEMU_BINARY"
    export P210_QEMU_BUILD_DIR="$QEMU_BUILD_DIR"
else
    # Never let source-only acceptance consume a coincidental or stale cache.
    NO_QEMU="$OUTPUT/no-qemu"
    mkdir "$NO_QEMU"
    export P210_QEMU_BINARY="$NO_QEMU/qemu-system-arm"
    export P210_QEMU_BUILD_DIR="$NO_QEMU"
fi

python3 -m compileall -q "$ROOT/src" "$ROOT/scripts" "$ROOT/tests" \
    "$ROOT/cosim/tests" "$FIRMWAVE_ROOT/src" "$FIRMWAVE_ROOT/scripts" \
    "$FIRMWAVE_ROOT/tests"
python3 "$ROOT/scripts/acceptance_gate.py" test-suite \
    --start-dir "$ROOT/tests" --label acceptance-reference \
    --summary "$OUTPUT/reference-tests.json" \
    --log "$OUTPUT/reference-tests.log" \
    --expect-skips 0 --min-tests 75 \
    --require-test test_acceptance_gate.AcceptanceGateTests.test_full_manifest_binds_source_qemu_tests_and_firmware_hashes \
    --require-test test_acceptance_gate.AcceptanceGateTests.test_full_acceptance_requires_clean_twin_and_firmwave \
    --require-test test_acceptance_gate.AcceptanceGateTests.test_hidden_index_flags_cannot_hide_source_changes \
    --require-test test_acceptance_gate.AcceptanceGateTests.test_no_build_cache_is_bound_to_sources_and_artifacts \
    --require-test test_appliance_smoke.ApplianceSmokeTests.test_complete_appliance_can_bind_every_local_contact_and_stop \
    --require-test test_firmwave_bundle.FirmwaveBundleTests.test_hashed_decoy_cannot_substitute_for_canonical_boot_kernel \
    --require-test test_firmwave_bundle.FirmwaveBundleTests.test_valid_bundle_binds_source_interface_and_every_artifact \
    --require-test test_firmwave_dependency.FirmwaveDependencyTests.test_explicit_checkout_resolves_exact_release_identity \
    --require-test test_firmwave_dependency.FirmwaveDependencyTests.test_lock_profile_is_required_and_exact \
    --require-test test_firmwave_dependency.FirmwaveDependencyTests.test_managed_cache_rejects_symlinked_parent_without_touching_target \
    --require-test test_firmwave_dependency.FirmwaveDependencyTests.test_skip_worktree_cannot_hide_a_modified_tracked_file \
    --require-test test_runtime_contacts.ContinuousPLContactTests.test_full_50mhz_dual_65536_fft_crosses_the_nsft_wire_contract \
    --require-test test_usb_contacts.USBCompositeContactTests.test_rndis_ethernet_and_tcp_proxy_reach_iiod
python3 "$ROOT/scripts/acceptance_gate.py" test-suite \
    --start-dir "$FIRMWAVE_ROOT/tests" --label acceptance-firmwave \
    --summary "$OUTPUT/firmwave-tests.json" \
    --log "$OUTPUT/firmwave-tests.log" \
    --expect-skips 0 --min-tests 24 \
    --require-test test_distribution.DistributionTests.test_sdist_manifest_covers_every_nonpackage_source_class \
    --require-test test_artifacts.ArtifactBoundaryTests.test_rootfs_paths_symlinks_and_uimage_crc_fail_closed \
    --require-test test_interface_manifest.RuntimeManifestTests.test_manifest_paths_are_relative_and_every_output_is_hashed \
    --require-test test_provenance_cli.CLITests.test_source_identity_outside_git_fails_without_a_traceback \
    --require-test test_provenance_cli.ProvenanceTests.test_hidden_index_flags_cannot_mask_worktree_changes \
    --require-test test_provenance_cli.ProvenanceTests.test_state_sha_exactly_matches_twin_acceptance_material_clean_and_dirty \
    --require-test test_source_boundaries.SourceBoundaryTests.test_no_twin_python_namespace_reference_remains
if [ "$FIRMWARE" = yes ]; then
    python3 "$ROOT/scripts/acceptance_gate.py" test-suite \
        --start-dir "$ROOT/cosim/tests" --label acceptance-live-cosim \
        --summary "$OUTPUT/cosim-tests.json" \
        --log "$OUTPUT/cosim-tests.log" \
        --expect-skips 0 --min-tests 23 \
        --require-test test_qemu_device_sources.QEMUDeviceSourceTests.test_sources_compile_with_qemu_10_flags \
        --require-test test_qemu_fft_source.P210FFTSourceTests.test_qemu_header_refines_the_canonical_firmwave_interface \
        --require-test test_qemu_fft_source.P210FFTSourceTests.test_integrated_qemu_executes_65536_bins_for_two_channels \
        --require-test test_qemu_fft_source.P210FFTSourceTests.test_integrated_qemu_executes_fft_and_rejects_overlap \
        --require-test test_qemu_sdr_live.P210SDRLiveTests.test_xsa_capabilities_alignment_and_pause_readback
else
    python3 "$ROOT/scripts/acceptance_gate.py" test-suite \
        --start-dir "$ROOT/cosim/tests" --label acceptance-source-cosim \
        --summary "$OUTPUT/cosim-tests.json" \
        --log "$OUTPUT/cosim-tests.log" \
        --expect-skips 15 \
        --expect-skip-reason '2:set P210_QEMU_BUILD_DIR to a configured QEMU 10.0.2 build' \
        --expect-skip-reason '13:set P210_QEMU_BINARY to an integrated P210 QEMU binary' \
        --skip-policy source-without-qemu \
        --min-tests 23 \
        --require-test test_qemu_fft_source.P210FFTSourceTests.test_qemu_header_refines_the_canonical_firmwave_interface
fi

for script in "$ROOT"/scripts/*.sh; do
    bash -n "$script"
done
for script in "$FIRMWAVE_ROOT"/scripts/*.sh; do
    bash -n "$script"
done

python3 "$FIRMWAVE_ROOT/scripts/test_firmware.py" \
    --cache-dir "$FIRMWARE_CACHE" --fetch --json \
    >"$OUTPUT/firmware-artifacts.json"
python3 -m neptunesdr_firmwave fetch p210-system-xsa \
    --cache-dir "$FIRMWARE_CACHE" --json >"$OUTPUT/xsa-fetch.json"
python3 -m neptunesdr_firmwave validate-xsa \
    --artifact p210-system-xsa --cache-dir "$FIRMWARE_CACHE" --json \
    >"$OUTPUT/xsa-validation.json"
python3 -m neptunesdr_twin contracts >"$OUTPUT/contracts.json"
python3 -m neptunesdr_twin fft-plan >"$OUTPUT/fft-plan.json"
python3 -m neptunesdr_twin appliance --dry-run \
    >"$OUTPUT/appliance-plan.json"
"$ROOT/scripts/run_virtual_appliance.sh" --no-build --dry-run \
    >"$OUTPUT/firmware-appliance-plan.txt"

if [ "$FIRMWARE" = yes ]; then
    export P210_RUNTIME_OUTPUT="$OUTPUT/runtime"
    if [ "$BUILD" = yes ]; then
        "$ROOT/scripts/run_p210_firmware.sh" \
            >"$OUTPUT/firmware-runtime.log" 2>&1
    else
        "$ROOT/scripts/run_p210_firmware.sh" --no-build \
            >"$OUTPUT/firmware-runtime.log" 2>&1
    fi
    grep -Fxq 'P210_RUNTIME PASS' "$OUTPUT/firmware-runtime.log"
fi

if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$ROOT" diff --check
    git -C "$FIRMWAVE_ROOT" diff --check
fi

if [ "$FIRMWARE" = yes ]; then
    python3 "$ROOT/scripts/acceptance_gate.py" finish-full \
        --root "$ROOT" --firmwave-root "$FIRMWAVE_ROOT" --output "$OUTPUT" \
        --reference-summary "$OUTPUT/reference-tests.json" \
        --firmwave-summary "$OUTPUT/firmwave-tests.json" \
        --cosim-summary "$OUTPUT/cosim-tests.json" \
        --qemu "$QEMU_BINARY" --qemu-build-dir "$QEMU_BUILD_DIR" \
        --firmware-log "$OUTPUT/firmware-runtime.log" \
        --runtime-dir "$OUTPUT/runtime"
    printf '%s\n' PASS >"$OUTPUT/status"
    acceptance_done=yes
    printf '%s\n' "$RUN_ID" >"$OUTPUT_BASE/latest-pass.part"
    mv "$OUTPUT_BASE/latest-pass.part" "$OUTPUT_BASE/latest-pass"
    printf '%s\n' 'NEPTUNE_TWIN_ACCEPTANCE PASS'
    printf 'manifest=%s\n' "$OUTPUT/acceptance-manifest.json"
else
    printf '%s\n' 'SOURCE_ONLY_PASS_WITH_15_QEMU_SKIPS' >"$OUTPUT/status"
    acceptance_done=yes
    printf '%s\n' 'NEPTUNE_TWIN_SOURCE_ACCEPTANCE PASS skipped=15'
fi
printf 'evidence=%s\n' "$OUTPUT"
