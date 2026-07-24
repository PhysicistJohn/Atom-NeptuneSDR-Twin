#!/usr/bin/env python3
"""Idempotently wire the RTL co-processor device into a patched QEMU tree.

Called by build_p210_qemu.sh AFTER wire_operator_device.py. Adds the meson
entry, the machine include, a `p210-rtl` machine property, and -- when that
property is on -- instantiates the dlopen-backed RTL block at the real
accelerator address 0x7c450000 (GIC SPI 58), taking precedence over the
operator and the v1 FFT. With the property off, nothing changes.

Usage: wire_rtl_device.py <qemu-source-dir>
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


# 1. meson: build the RTL device
patch(src + "/hw/misc/meson.build", [(
    "p210_rtl.c",
    lambda s: s.replace(
        "system_ss.add(when: 'CONFIG_ZYNQ', if_true: files('p210_operator.c'))",
        "system_ss.add(when: 'CONFIG_ZYNQ', if_true: files('p210_operator.c'))\n"
        "system_ss.add(when: 'CONFIG_ZYNQ', if_true: files('p210_rtl.c'))"),
)])

mach = src + "/hw/arm/xilinx_zynq.c"

# 2. include
patch(mach, [(
    "p210_rtl.h",
    lambda s: s.replace('#include "hw/misc/p210_operator.h"',
                        '#include "hw/misc/p210_operator.h"\n#include "hw/misc/p210_rtl.h"'),
)])

# 3. machine state field
patch(mach, [(
    "bool p210_rtl;",
    lambda s: s.replace("    bool p210_operator;\n",
                        "    bool p210_operator;\n    bool p210_rtl;\n"),
)])

# 4. get/set accessors (inserted after zynq_set_p210_operator)
GETSET = '''
static bool zynq_get_p210_rtl(Object *obj, Error **errp)
{
    return ZYNQ_MACHINE(obj)->p210_rtl;
}

static void zynq_set_p210_rtl(Object *obj, bool value, Error **errp)
{
    ZYNQ_MACHINE(obj)->p210_rtl = value;
}
'''
patch(mach, [(
    "zynq_get_p210_rtl",
    lambda s: s.replace(
        "static void zynq_set_p210_operator(Object *obj, bool value, Error **errp)\n"
        "{\n    ZYNQ_MACHINE(obj)->p210_operator = value;\n}\n",
        "static void zynq_set_p210_operator(Object *obj, bool value, Error **errp)\n"
        "{\n    ZYNQ_MACHINE(obj)->p210_operator = value;\n}\n" + GETSET),
)])

# 5. property registration (after the "p210-operator" property block)
PROP = '''
    object_class_property_add_bool(oc, "p210-rtl", zynq_get_p210_rtl,
                                   zynq_set_p210_rtl);
    object_class_property_set_description(oc, "p210-rtl",
                                          "Run a Verilated RTL block at 0x7c450000 from $P210_RTL_LIB (implies p210)");
'''
patch(mach, [(
    '"p210-rtl"',
    lambda s: s.replace(
        '    object_class_property_set_description(oc, "p210-operator",\n'
        '                                          "Map the v2 spectral operator at 0x7c450000 (implies p210)");\n',
        '    object_class_property_set_description(oc, "p210-operator",\n'
        '                                          "Map the v2 spectral operator at 0x7c450000 (implies p210)");\n' + PROP),
)])

# 6. instantiation: extend the operator/FFT choice into rtl/operator/FFT.
TWO_WAY = (
    "        if (zynq_machine->p210_operator) {\n"
    "            dev = qdev_new(TYPE_P210_OPERATOR);\n"
    "        } else {\n"
    "            dev = qdev_new(TYPE_P210_FFT);\n"
    "        }\n"
)
THREE_WAY = (
    "        if (zynq_machine->p210_rtl) {\n"
    "            dev = qdev_new(TYPE_P210_RTL);\n"
    "        } else if (zynq_machine->p210_operator) {\n"
    "            dev = qdev_new(TYPE_P210_OPERATOR);\n"
    "        } else {\n"
    "            dev = qdev_new(TYPE_P210_FFT);\n"
    "        }\n"
)
s = open(mach).read()
if "zynq_machine->p210_rtl) {" not in s and TWO_WAY in s:
    s = s.replace(TWO_WAY, THREE_WAY)
    open(mach, "w").write(s)
print("wired p210-rtl into", mach)
