/*
 * Minimal register-visible AD9361 SSI model for the NeptuneSDR P210.
 *
 * This model is deliberately aimed at executing the unmodified ADI Linux
 * driver.  It implements the wire instruction, writable register file,
 * identity, immediate calibration completion, synthesizer lock contacts and
 * ENSM state transitions.  It does not model RF impairments or sample data;
 * those belong behind the P210 PL model/co-simulation boundary.
 *
 * Copyright (c) 2026 NeptuneSDR Twin contributors
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "hw/misc/p210_sdr.h"
#include "hw/ssi/ssi.h"
#include "migration/vmstate.h"
#include "qemu/module.h"

#define AD9361_REG_COUNT                 0x400
#define AD9361_REG_SPI_CONF              0x000
#define AD9361_REG_ENSM_MODE             0x013
#define AD9361_REG_ENSM_CONFIG_1         0x014
#define AD9361_REG_CALIBRATION_CTRL      0x016
#define AD9361_REG_STATE                 0x017
#define AD9361_REG_PRODUCT_ID            0x037
#define AD9361_REG_CH_1_OVERFLOW         0x05e
#define AD9361_REG_FRACT_BB_FREQ_1       0x041
#define AD9361_REG_FRACT_BB_FREQ_2       0x042
#define AD9361_REG_FRACT_BB_FREQ_3       0x043
#define AD9361_REG_INTEGER_BB_FREQ       0x044
#define AD9361_REG_RX_BBF_R2346          0x1e6
#define AD9361_REG_RX_BBF_C3_MSB         0x1eb
#define AD9361_REG_RX_BBF_C3_LSB         0x1ec
#define AD9361_REG_RX_BBF_TUNE_DIVIDE    0x1f8
#define AD9361_REG_RX_BBF_TUNE_CONFIG    0x1f9
#define AD9361_REG_RX_CAL_STATUS         0x244
#define AD9361_REG_RX_VCO_LOCK           0x247
#define AD9361_REG_TX_CAL_STATUS         0x284
#define AD9361_REG_TX_VCO_LOCK           0x287

#define AD9361_PRODUCT_ID                0x0a
#define AD9361_BBPLL_LOCK                BIT(7)
#define AD9361_CP_CAL_VALID              BIT(7)
#define AD9361_VCO_LOCK                  BIT(1)
#define AD9361_RX_BB_TUNE_CAL            BIT(7)
#define AD9361_P210_REFCLK_HZ            40000000ULL

#define AD9361_ENSM_ALERT                0x05
#define AD9361_ENSM_TX                   0x06
#define AD9361_ENSM_RX                   0x08
#define AD9361_ENSM_FDD                  0x0a

#define AD9361_FORCE_RX                  BIT(6)
#define AD9361_FORCE_TX                  BIT(5)
#define AD9361_FORCE_ALERT               BIT(2)
#define AD9361_TO_ALERT                  BIT(0)
#define AD9361_FDD_MODE                  BIT(0)

typedef struct P210AD9361State {
    SSIPeripheral parent_obj;
    uint8_t regs[AD9361_REG_COUNT];
    uint16_t instruction;
    uint16_t address;
    uint8_t position;
    uint8_t count;
    uint8_t data_index;
    uint8_t ensm_state;
    bool write;
} P210AD9361State;

DECLARE_INSTANCE_CHECKER(P210AD9361State, P210_AD9361, TYPE_P210_AD9361)

static void p210_ad9361_reset_registers(P210AD9361State *s)
{
    memset(s->regs, 0, sizeof(s->regs));
    s->regs[AD9361_REG_PRODUCT_ID] = AD9361_PRODUCT_ID;
    s->ensm_state = AD9361_ENSM_ALERT;
    s->instruction = 0;
    s->address = 0;
    s->position = 0;
    s->count = 0;
    s->data_index = 0;
    s->write = false;
}

static void p210_ad9361_reset(DeviceState *dev)
{
    p210_ad9361_reset_registers(P210_AD9361(dev));
}

static uint8_t p210_ad9361_read(P210AD9361State *s, uint16_t reg)
{
    switch (reg) {
    case AD9361_REG_CALIBRATION_CTRL:
        /* The functional model completes each requested calibration. */
        return 0;
    case AD9361_REG_STATE:
        return s->ensm_state;
    case AD9361_REG_PRODUCT_ID:
        return AD9361_PRODUCT_ID;
    case AD9361_REG_CH_1_OVERFLOW:
        return s->regs[reg] | AD9361_BBPLL_LOCK;
    case AD9361_REG_RX_CAL_STATUS:
    case AD9361_REG_TX_CAL_STATUS:
        return s->regs[reg] | AD9361_CP_CAL_VALID;
    case AD9361_REG_RX_VCO_LOCK:
    case AD9361_REG_TX_VCO_LOCK:
        return s->regs[reg] | AD9361_VCO_LOCK;
    default:
        return s->regs[reg];
    }
}

/*
 * Complete the on-chip RX baseband-filter tuner.
 *
 * The Linux driver starts this calibration after programming the BBPLL and
 * RX tune divider.  Real silicon then writes the R2346 and C3 component codes
 * consumed by ad9361_rx_tia_calib() and ad9361_rx_adc_setup().  Returning a
 * cleared calibration bit without producing those codes is not a successful
 * calibration and makes the real driver divide by zero.
 *
 * Component codes vary slightly by die.  The exact P210 default profile
 * (983.04 MHz / 9, effective BBBW 8.606895 MHz) uses a representative value
 * from ADI hardware traces.  Other bandwidths solve the same inverse-RC
 * equation used by the ADI driver for a typical 0.96e6 calibration result.
 */
static void p210_ad9361_complete_rx_bbf_cal(P210AD9361State *s)
{
    uint64_t fract = ((uint64_t)s->regs[AD9361_REG_FRACT_BB_FREQ_1] << 16) |
                     ((uint64_t)s->regs[AD9361_REG_FRACT_BB_FREQ_2] << 8) |
                     s->regs[AD9361_REG_FRACT_BB_FREQ_3];
    uint64_t bbpll_hz = AD9361_P210_REFCLK_HZ *
                        s->regs[AD9361_REG_INTEGER_BB_FREQ] +
                        (AD9361_P210_REFCLK_HZ * fract >> 24);
    uint32_t divide = s->regs[AD9361_REG_RX_BBF_TUNE_DIVIDE] |
                      ((s->regs[AD9361_REG_RX_BBF_TUNE_CONFIG] & 1) << 8);
    uint64_t bw_hz;
    uint64_t correction;
    uint64_t denominator;
    uint64_t component_product;
    uint64_t capacitance_ff;
    uint32_t steps;
    uint8_t r2346;
    uint8_t c3_msb;
    uint8_t c3_lsb;

    if (!divide || !bbpll_hz) {
        /* A malformed setup must still remain finite for diagnostic reads. */
        divide = MAX(divide, 1U);
        bbpll_hz = MAX(bbpll_hz, AD9361_P210_REFCLK_HZ);
    }
    bw_hz = bbpll_hz * 10000ULL / (126906ULL * divide);

    /* ADI traces for this exact 18 MHz complex-bandwidth startup profile
     * show C3_MSB=0, C3_LSB=0x35..0x3b and R2346=1. */
    if (divide == 9 && bbpll_hz >= 982000000ULL &&
        bbpll_hz <= 984000000ULL) {
        s->regs[AD9361_REG_RX_BBF_R2346] = 1;
        s->regs[AD9361_REG_RX_BBF_C3_MSB] = 0;
        s->regs[AD9361_REG_RX_BBF_C3_LSB] = 0x36;
        return;
    }

    correction = 1000;
    if (bw_hz >= 18000000ULL) {
        correction += 10 * ((bw_hz - 18000000ULL) / 1000000ULL);
    }

    /* Driver equation:
     * invrc = 160975 * R * C * BW * correction / 1e12.
     * Solve for R*C with a representative invrc of 0.96e6. */
    denominator = 160975ULL * bw_hz * correction;
    component_product = (960000000000000000ULL + denominator / 2) /
                        denominator;

    if (bw_hz <= 1500000ULL) {
        r2346 = 4;
    } else if (bw_hz <= 3500000ULL) {
        r2346 = 2;
    } else {
        r2346 = 1;
    }
    capacitance_ff = (component_product + r2346 / 2) / r2346;
    capacitance_ff = MAX(140ULL, MIN(capacitance_ff, 11490ULL));
    steps = (capacitance_ff - 140 + 5) / 10;

    /* Prefer the observed MSB=0 encoding whenever the LSB field can hold it.
     * Otherwise both fields contribute in 160 fF and 10 fF steps. */
    if (steps <= 127) {
        c3_msb = 0;
        c3_lsb = steps;
    } else {
        c3_msb = MIN(63U, steps / 16);
        c3_lsb = MIN(127U, steps - c3_msb * 16);
    }

    s->regs[AD9361_REG_RX_BBF_R2346] = r2346;
    s->regs[AD9361_REG_RX_BBF_C3_MSB] = c3_msb;
    s->regs[AD9361_REG_RX_BBF_C3_LSB] = c3_lsb;
}

static void p210_ad9361_write(P210AD9361State *s, uint16_t reg, uint8_t value)
{
    if (reg == AD9361_REG_PRODUCT_ID || reg == AD9361_REG_STATE) {
        return;
    }

    if (reg == AD9361_REG_SPI_CONF && (value & (BIT(7) | BIT(0)))) {
        p210_ad9361_reset_registers(s);
        return;
    }

    if (reg == AD9361_REG_CALIBRATION_CTRL) {
        if (value & AD9361_RX_BB_TUNE_CAL) {
            p210_ad9361_complete_rx_bbf_cal(s);
        }
        return;
    }
    s->regs[reg] = value;

    if (reg == AD9361_REG_ENSM_CONFIG_1) {
        /* FORCE_ALERT wins.  TO_ALERT is also set during normal forced
         * RX/TX/FDD transitions, so it must not be tested first. */
        if (value & AD9361_FORCE_ALERT) {
            s->ensm_state = AD9361_ENSM_ALERT;
        } else if (value & AD9361_FORCE_RX) {
            s->ensm_state = AD9361_ENSM_RX;
        } else if (value & AD9361_FORCE_TX) {
            s->ensm_state =
                (s->regs[AD9361_REG_ENSM_MODE] & AD9361_FDD_MODE) ?
                AD9361_ENSM_FDD : AD9361_ENSM_TX;
        } else if (value & AD9361_TO_ALERT) {
            s->ensm_state = AD9361_ENSM_ALERT;
        }
    } else if (reg == AD9361_REG_ENSM_MODE && (value & AD9361_FDD_MODE)) {
        s->ensm_state = AD9361_ENSM_FDD;
    }
}

static uint32_t p210_ad9361_transfer(SSIPeripheral *peripheral, uint32_t value)
{
    P210AD9361State *s = P210_AD9361(peripheral);
    uint8_t tx = value;
    uint8_t rx = 0;
    uint16_t reg;

    if (s->position == 0) {
        s->instruction = (uint16_t)tx << 8;
    } else if (s->position == 1) {
        s->instruction |= tx;
        s->write = s->instruction & BIT(15);
        s->count = ((s->instruction >> 12) & 0x7) + 1;
        s->address = s->instruction & 0x3ff;
        s->data_index = 0;
    } else if (s->data_index < s->count) {
        reg = (s->address - s->data_index) & 0x3ff;
        if (s->write) {
            p210_ad9361_write(s, reg, tx);
        } else {
            rx = p210_ad9361_read(s, reg);
        }
        s->data_index++;
    }

    s->position++;
    return rx;
}

static int p210_ad9361_set_cs(SSIPeripheral *peripheral, bool level)
{
    P210AD9361State *s = P210_AD9361(peripheral);

    /* Active-low CS: either edge starts a clean wire transaction. */
    s->instruction = 0;
    s->position = 0;
    s->count = 0;
    s->data_index = 0;
    s->write = false;
    return 0;
}

static const VMStateDescription vmstate_p210_ad9361 = {
    .name = TYPE_P210_AD9361,
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_SSI_PERIPHERAL(parent_obj, P210AD9361State),
        VMSTATE_UINT8_ARRAY(regs, P210AD9361State, AD9361_REG_COUNT),
        VMSTATE_UINT16(instruction, P210AD9361State),
        VMSTATE_UINT16(address, P210AD9361State),
        VMSTATE_UINT8(position, P210AD9361State),
        VMSTATE_UINT8(count, P210AD9361State),
        VMSTATE_UINT8(data_index, P210AD9361State),
        VMSTATE_UINT8(ensm_state, P210AD9361State),
        VMSTATE_BOOL(write, P210AD9361State),
        VMSTATE_END_OF_LIST()
    },
};

static void p210_ad9361_realize(SSIPeripheral *peripheral, Error **errp)
{
    p210_ad9361_reset_registers(P210_AD9361(peripheral));
}

static void p210_ad9361_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    SSIPeripheralClass *ssc = SSI_PERIPHERAL_CLASS(klass);

    device_class_set_legacy_reset(dc, p210_ad9361_reset);
    dc->vmsd = &vmstate_p210_ad9361;
    ssc->realize = p210_ad9361_realize;
    ssc->transfer = p210_ad9361_transfer;
    ssc->set_cs = p210_ad9361_set_cs;
    ssc->cs_polarity = SSI_CS_LOW;
}

static const TypeInfo p210_ad9361_info = {
    .name = TYPE_P210_AD9361,
    .parent = TYPE_SSI_PERIPHERAL,
    .instance_size = sizeof(P210AD9361State),
    .class_init = p210_ad9361_class_init,
};

static void p210_ad9361_register_types(void)
{
    type_register_static(&p210_ad9361_info);
}

type_init(p210_ad9361_register_types)
