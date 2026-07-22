#!/usr/bin/env bash
set -euo pipefail

ATOM_NEPTUNE_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
FIRMWAVE_ROOT=$(python3 "$ATOM_NEPTUNE_ROOT/scripts/resolve_firmwave.py")
export NEPTUNESDR_FIRMWAVE_ROOT="$FIRMWAVE_ROOT"
export PYTHONPATH="$FIRMWAVE_ROOT/src:$ATOM_NEPTUNE_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
OUTPUT=${P210_SOURCE_GATE_OUTPUT:-"$ATOM_NEPTUNE_ROOT/.cache/source-gate"}
mkdir -p "$OUTPUT"
rm -f "$OUTPUT/reference-tests.json" "$OUTPUT/firmwave-tests.json" \
  "$OUTPUT/cosim-tests.json"

# The ordinary source gate must not accidentally execute an unrelated or stale
# optional QEMU build from .cache. Integration runs set these paths explicitly;
# here an empty private directory makes those tests report a deliberate skip.
ATOM_NEPTUNE_NO_QEMU=$(mktemp -d "${TMPDIR:-/tmp}/atom-neptune-source-gate.XXXXXX")
trap 'rmdir "$ATOM_NEPTUNE_NO_QEMU"' EXIT HUP INT TERM
export P210_QEMU_BUILD_DIR="$ATOM_NEPTUNE_NO_QEMU"
export P210_QEMU_BINARY="$ATOM_NEPTUNE_NO_QEMU/qemu-system-arm"

cd "$ATOM_NEPTUNE_ROOT"
python3 -m compileall -q src scripts tests cosim/tests \
  "$FIRMWAVE_ROOT/src" "$FIRMWAVE_ROOT/scripts" "$FIRMWAVE_ROOT/tests"
python3 scripts/acceptance_gate.py test-suite \
  --start-dir tests \
  --label source-reference \
  --summary "$OUTPUT/reference-tests.json" \
  --expect-skips 0 \
  --min-tests 75 \
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
python3 scripts/acceptance_gate.py test-suite \
  --start-dir "$FIRMWAVE_ROOT/tests" \
  --label source-firmwave \
  --summary "$OUTPUT/firmwave-tests.json" \
  --expect-skips 0 \
  --min-tests 24 \
  --require-test test_distribution.DistributionTests.test_sdist_manifest_covers_every_nonpackage_source_class \
  --require-test test_artifacts.ArtifactBoundaryTests.test_rootfs_paths_symlinks_and_uimage_crc_fail_closed \
  --require-test test_interface_manifest.RuntimeManifestTests.test_manifest_paths_are_relative_and_every_output_is_hashed \
  --require-test test_provenance_cli.CLITests.test_source_identity_outside_git_fails_without_a_traceback \
  --require-test test_provenance_cli.ProvenanceTests.test_hidden_index_flags_cannot_mask_worktree_changes \
  --require-test test_provenance_cli.ProvenanceTests.test_state_sha_exactly_matches_twin_acceptance_material_clean_and_dirty \
  --require-test test_source_boundaries.SourceBoundaryTests.test_no_twin_python_namespace_reference_remains
python3 scripts/acceptance_gate.py test-suite \
  --start-dir cosim/tests \
  --label source-cosim \
  --summary "$OUTPUT/cosim-tests.json" \
  --expect-skips 15 \
  --expect-skip-reason '2:set P210_QEMU_BUILD_DIR to a configured QEMU 10.0.2 build' \
  --expect-skip-reason '13:set P210_QEMU_BINARY to an integrated P210 QEMU binary' \
  --skip-policy source-without-qemu \
  --min-tests 23 \
  --require-test test_qemu_fft_source.P210FFTSourceTests.test_qemu_header_refines_the_canonical_firmwave_interface

for script in scripts/*.sh; do
  bash -n "$script"
done
for script in "$FIRMWAVE_ROOT"/scripts/*.sh; do
  bash -n "$script"
done

python3 -m neptunesdr_twin contracts >/dev/null
python3 -m neptunesdr_twin fft-plan >/dev/null
python3 -m neptunesdr_twin serve --dry-run >/dev/null
python3 -m neptunesdr_twin usbip-serve --dry-run >/dev/null
python3 -m neptunesdr_twin appliance --dry-run >/dev/null
python3 -m neptunesdr_firmwave interface --json >/dev/null
python3 -m neptunesdr_firmwave validate-locks --json >/dev/null
./scripts/linux_usb_gadget.sh >/dev/null
git diff --check
git -C "$FIRMWAVE_ROOT" diff --check

printf '%s\n' \
  'ATOM_NEPTUNE_SOURCE_GATE PASS (15 live/configured-QEMU tests intentionally skipped)'
printf 'summaries=%s\n' "$OUTPUT"
