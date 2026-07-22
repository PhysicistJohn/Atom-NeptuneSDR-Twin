# Upstream disposition

The split produced one warranted **future** upstream QEMU series, one possible
generic follow-on, and no patch that should be submitted verbatim today. No
upstream change is required to build, boot, or use the two-repository system.

## QEMU: prepare a generic Zynq A9 secondary-reset/release series

The work exposed a reusable gap around Zynq-7000's second Cortex-A9 reset,
start-address, and release behavior. That is useful outside Neptune/P210 and is
the strongest upstream candidate. The current
`qemu/patches/0001-p210-zynq-devices.patch` is nevertheless a board-integration
patch, not a reviewable upstream series. Do **not** send it verbatim.

Before posting:

1. rebase on current QEMU `master` and confirm the generic defect still exists;
2. reduce the change to Zynq A9 reset/release behavior, with no P210 machine,
   deterministic tones, FFT, diagnostic register, or seller-specific address
   assumptions;
3. add a focused qtest that fails before the fix and exercises reset, release,
   restart, and migration/reset state as applicable;
4. split mechanical, functional, and test changes into reviewable commits and
   run QEMU's style and target test suites; and
5. document the behavior from a public Zynq specification or an existing
   upstream-supported guest, then use QEMU's normal mailing-list review path.

That is a real upstream work item, but it is not yet a ready pull request.

## QEMU/ADI: generic AXI-DMAC may become a later series

The four-entry descriptor queue, transfer-ID reuse, register-width behavior,
capability-dependent registers, and migration/reset semantics could eventually
support a generic AXI-DMAC model. The present implementation is coupled to the
P210 machine and deterministic sample source, so it first needs a standalone
device API, public ADI register-spec traceability, qtests independent of P210
firmware, and confirmation that an upstream machine or maintained test user
benefits. Until then it remains Twin code, not an upstream-ready change.

## Community P210 material: open an issue before proposing code

The public `wucke13/Neptune-SDR-nix-utils` artifacts are the source of the
kernel/device-tree/XSA evidence consumed by Firmwave. The useful upstream action
is an evidence-focused issue first: ask for the exact P210 hardware revision,
artifact provenance/build recipe, the missing full rootfs or factory-image
boundary, and clarification of the reported non-functional AD9361 path. A code
PR would be premature until the maintainer confirms the intended scope and a
physical revision can reproduce the change.

## No current PR for libiio, PlutoSDR firmware, ADI HDL, or OpenWiFi

- **libiio:** the pinned official client and released guest `iiod` interoperate;
  no generic library defect was isolated.
- **Analog Devices `plutosdr-fw`:** Firmwave reuses the official v0.39 rootfs as
  a hash-locked compatibility input. The P210 composition is not a PlutoSDR
  firmware bug or a replacement image to contribute upstream.
- **Analog Devices HDL:** the proposed FFT ABI and QEMU model are not synthesized
  RTL, and there is no resource/timing/CDC evidence suitable for an HDL PR.
- **OpenWiFi:** Neptune/P210 support is already present upstream and marked
  unofficial. This work produced no physical-board result or generic fix to add;
  arrival evidence may justify a documentation or compatibility update later.

## Keep in the two repositories

These changes express Neptune/P210-specific contracts and should remain local:

- the P210 QEMU machine option, address/IRQ map, behavioral AD9361/CF-AXI,
  deterministic RF tones, proposed FFT, and acceptance-only diagnostics;
- the observed USB reference profile and USB/IP appliance;
- the contract, evidence, orchestration, and arrival-differential machinery in
  the Twin;
- the ARM NSFT streamer, immutable input locks, XSA audit, canonical FFT ABI,
  and non-flashable public-P210-kernel plus Pluto-v0.39-rootfs composition in
  [`Atom-NeptuneSDR_Firmwave`](https://github.com/PhysicistJohn/Atom-NeptuneSDR_Firmwave).

The proposed FFT is not present in the pinned public bitstream. Upstreaming its
model as a statement about shipped P210 hardware would be misleading.

## Not an upstream bug report

The public P210 device tree's host-mode USB controller and Pluto userspace's
gadget expectation conflict in this development composition. That is not by
itself a QEMU defect: QEMU is not obligated to invent a peripheral-mode PHY for
a host-mode board description. The USB/IP adapter closes the virtual protocol
contact without falsifying the device tree.

Likewise, storefront contradictions about DDR size, CPU clock, RF chip, and
host throughput belong in the P210 evidence profile. They are not defects in
Linux, libiio, QEMU, or Analog Devices documentation.

## Decision rule

Submit an upstream fix only when all four are true:

1. the fault is in upstream-owned generic behavior rather than the Twin or
   Firmwave contract;
2. a minimal public reproducer fails without P210-specific artifacts;
3. the correction has a focused regression test and preserves migration/API
   compatibility where required; and
4. the change is useful beyond these repositories.

Until those gates pass, a source-pinned, tested local implementation is the
more auditable engineering result.
