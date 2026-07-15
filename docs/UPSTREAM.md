# Upstream disposition

No upstream change is required to build, boot, or use this twin. The private
repository carries every patch it needs and pins the source revision to which
the patch applies.

## Keep in this repository

These changes express Neptune/P210-specific contracts and should not be pushed
into a generic project as though they were universal hardware:

- the P210 machine option and address/IRQ map;
- the behavioral AD9361, CF-AXI, AXI-DMAC and proposed FFT devices;
- deterministic RF tones and acceptance-only diagnostic registers;
- the ARM NSFT streamer and its reserved-memory test layout;
- the public-P210-kernel plus Pluto-v0.39-rootfs composition;
- the observed USB reference profile and USB/IP appliance; and
- the contract, evidence and arrival-differential machinery.

The proposed FFT is not present in the pinned public bitstream. Upstreaming its
model as a statement about shipped P210 hardware would be misleading.

## Possible generic candidates

A change should be proposed upstream only if it can be split from the P210
machine, reproduced on an upstream-supported configuration, and tested without
private or seller-specific assumptions. The current candidates are:

- any independently reproducible Zynq secondary-CPU reset/release or migration
  state defect exposed by the two-Cortex-A9 boot; and
- generic AXI-DMAC behavior corrections that can be demonstrated against the
  public ADI register specification and an upstream machine/device user.

Neither candidate is a required push today. Before proposing one, reduce it to
a minimal test against the current upstream QEMU tree, confirm it is not
already fixed, split it from the P210 devices, follow QEMU coding style and
licensing, and send it through QEMU's normal review path. The private twin can
continue using its pinned integration while that review happens.

## Not an upstream bug report

The public P210 device tree's host-mode USB controller and Pluto userspace's
gadget expectation are incompatible in this composition. That is not by itself
an upstream QEMU defect: QEMU is not obligated to invent a peripheral-mode PHY
for a host-mode board description. The USB/IP transport adapter is the honest
virtual-appliance solution.

Likewise, storefront contradictions about DDR size, CPU clock, RF chip and
host throughput belong in the P210 evidence profile. They are not defects in
Linux, libiio, QEMU or Analog Devices documentation.

## Decision rule

Push a fix upstream when all four are true:

1. the fault is in upstream-owned generic behavior rather than the twin's
   chosen model;
2. a minimal public reproducer fails without P210-private artifacts;
3. the correction has a focused regression test and preserves migration/API
   compatibility where required; and
4. the change is useful beyond this repository.

Until then, retaining a tested, source-pinned patch here is the more auditable
engineering choice.
