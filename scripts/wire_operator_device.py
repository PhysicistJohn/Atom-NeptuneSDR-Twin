#!/usr/bin/env python3
"""Idempotently wire the v2 operator device into a patched QEMU 10.0.2 tree.

Called by build_p210_qemu.sh after the base 0001 patch. Adds the meson build
entry, the machine include, a `p210-operator` machine property, and -- when that
property is on -- instantiates the operator at the real accelerator address
0x7c450000 (GIC SPI 58) in place of the v1 FFT, so the twin is faithful to the
hardware address. The default (property off) leaves the v1 FFT unchanged.

Usage: wire_operator_device.py <qemu-source-dir>
"""
import sys

src = sys.argv[1]


def patch(path, transforms):
    s = open(path).read()
    orig = s
    for guard, fn in transforms:
        if guard not in s:
            s = fn(s)
    if s != orig:
        open(path, "w").write(s)


# 1. meson: build the operator device
patch(src + "/hw/misc/meson.build", [(
    "p210_operator.c",
    lambda s: s.replace(
        "system_ss.add(when: 'CONFIG_ZYNQ', if_true: files('p210_fft.c'))",
        "system_ss.add(when: 'CONFIG_ZYNQ', if_true: files('p210_fft.c'))\n"
        "system_ss.add(when: 'CONFIG_ZYNQ', if_true: files('p210_operator.c'))"),
)])

mach = src + "/hw/arm/xilinx_zynq.c"

# 2. include
patch(mach, [(
    "p210_operator.h",
    lambda s: s.replace('#include "hw/misc/p210_fft.h"',
                        '#include "hw/misc/p210_fft.h"\n#include "hw/misc/p210_operator.h"'),
)])

# 3. machine state field
patch(mach, [(
    "bool p210_operator;",
    lambda s: s.replace("    bool p210;\n", "    bool p210;\n    bool p210_operator;\n"),
)])

# 4. get/set accessors (inserted after zynq_set_p210)
GETSET = '''
static bool zynq_get_p210_operator(Object *obj, Error **errp)
{
    return ZYNQ_MACHINE(obj)->p210_operator;
}

static void zynq_set_p210_operator(Object *obj, bool value, Error **errp)
{
    ZYNQ_MACHINE(obj)->p210_operator = value;
}
'''
patch(mach, [(
    "zynq_get_p210_operator",
    lambda s: s.replace(
        "static void zynq_set_p210(Object *obj, bool value, Error **errp)\n"
        "{\n    ZYNQ_MACHINE(obj)->p210 = value;\n}\n",
        "static void zynq_set_p210(Object *obj, bool value, Error **errp)\n"
        "{\n    ZYNQ_MACHINE(obj)->p210 = value;\n}\n" + GETSET),
)])

# 5. property registration (after the "p210" bool property)
PROP = '''
    object_class_property_add_bool(oc, "p210-operator", zynq_get_p210_operator,
                                   zynq_set_p210_operator);
    object_class_property_set_description(oc, "p210-operator",
                                          "Map the v2 spectral operator at 0x7c450000 (implies p210)");
'''
patch(mach, [(
    '"p210-operator"',
    lambda s: s.replace(
        '    object_class_property_set_description(oc, "p210",\n'
        '                                          "Enable HAMGEEK P210 SDR devices");\n',
        '    object_class_property_set_description(oc, "p210",\n'
        '                                          "Enable HAMGEEK P210 SDR devices");\n' + PROP),
)])

# 6. instantiation: operator at 0x7c450000 when p210_operator, else v1 FFT.
FFT_BLOCK = (
    "        dev = qdev_new(TYPE_P210_FFT);\n"
    "        busdev = SYS_BUS_DEVICE(dev);\n"
    "        sysbus_realize_and_unref(busdev, &error_fatal);\n"
    "        sysbus_mmio_map(busdev, 0, 0x7c450000);\n"
    "        sysbus_connect_irq(busdev, 0, pic[58]);\n"
)
COND_BLOCK = (
    "        if (zynq_machine->p210_operator) {\n"
    "            dev = qdev_new(TYPE_P210_OPERATOR);\n"
    "        } else {\n"
    "            dev = qdev_new(TYPE_P210_FFT);\n"
    "        }\n"
    "        busdev = SYS_BUS_DEVICE(dev);\n"
    "        sysbus_realize_and_unref(busdev, &error_fatal);\n"
    "        sysbus_mmio_map(busdev, 0, 0x7c450000);\n"
    "        sysbus_connect_irq(busdev, 0, pic[58]);\n"
)
# strip any prior standalone operator-at-0x7c460000 block from earlier builds
STALE = (
    "\n        dev = qdev_new(TYPE_P210_OPERATOR);\n"
    "        busdev = SYS_BUS_DEVICE(dev);\n"
    "        sysbus_realize_and_unref(busdev, &error_fatal);\n"
    "        sysbus_mmio_map(busdev, 0, 0x7c460000);\n"
    "        sysbus_connect_irq(busdev, 0, pic[59]);\n"
)
s = open(mach).read()
if STALE in s:
    s = s.replace(STALE, "")
if "zynq_machine->p210_operator) {" not in s and FFT_BLOCK in s:
    s = s.replace(FFT_BLOCK, COND_BLOCK)
open(mach, "w").write(s)
print("wired p210-operator into", mach)
