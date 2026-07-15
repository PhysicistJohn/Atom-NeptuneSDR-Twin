/*
 * NeptuneSDR programmable-logic FFT accelerator QEMU type.
 *
 * This is a firmware-visible functional contract for the proposed P210 PL
 * accelerator.  It is not a claim that the public/vendor bitstream contains
 * the block or that an equivalent RTL implementation has closed timing.
 *
 * Copyright (c) 2026 NeptuneSDR Twin contributors
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#ifndef HW_MISC_P210_FFT_H
#define HW_MISC_P210_FFT_H

#define TYPE_P210_FFT "p210-fft"
#define P210_FFT_MMIO_SIZE 0x1000

/* Stable firmware ABI, version 1.0.  Every register is 32-bit little-endian. */
#define P210_FFT_ID                         0x5446464eU /* "NFFT" in LE */
#define P210_FFT_VERSION                    0x00010000U

#define P210_FFT_REG_ID                     0x000
#define P210_FFT_REG_VERSION                0x004
#define P210_FFT_REG_CAPABILITIES           0x008
#define P210_FFT_REG_CONTROL                0x00c
#define P210_FFT_REG_STATUS                 0x010
#define P210_FFT_REG_ERROR_CODE             0x014
#define P210_FFT_REG_LOG2_N                 0x018
#define P210_FFT_REG_CHANNEL_COUNT          0x01c
#define P210_FFT_REG_CHANNEL_MASK           0x020
#define P210_FFT_REG_INPUT_ADDR             0x024
#define P210_FFT_REG_INPUT_BYTES            0x028
#define P210_FFT_REG_OUTPUT_ADDR            0x02c
#define P210_FFT_REG_OUTPUT_BYTES           0x030
#define P210_FFT_REG_SEQUENCE               0x034
#define P210_FFT_REG_RESULT_SEQUENCE        0x038
#define P210_FFT_REG_COMPLETED_LO           0x03c
#define P210_FFT_REG_COMPLETED_HI           0x040
#define P210_FFT_REG_ERROR_COUNT_LO         0x044
#define P210_FFT_REG_ERROR_COUNT_HI         0x048
#define P210_FFT_REG_BINS_WRITTEN           0x04c
#define P210_FFT_REG_MIN_LOG2_N             0x050
#define P210_FFT_REG_MAX_LOG2_N             0x054

#define P210_FFT_CONTROL_START              (1U << 0)
#define P210_FFT_CONTROL_SOFT_RESET         (1U << 1)
#define P210_FFT_CONTROL_IRQ_ENABLE         (1U << 8)

#define P210_FFT_STATUS_BUSY                (1U << 0)
#define P210_FFT_STATUS_DONE                (1U << 1)
#define P210_FFT_STATUS_ERROR               (1U << 2)
#define P210_FFT_STATUS_IRQ_PENDING         (1U << 3)

#define P210_FFT_CAP_IQ16_LE                (1U << 0)
#define P210_FFT_CAP_POWER_U32_LE           (1U << 1)
#define P210_FFT_CAP_TWO_CHANNEL            (1U << 2)
#define P210_FFT_CAP_SCALE_EACH_STAGE       (1U << 3)
#define P210_FFT_CAP_NATURAL_ORDER          (1U << 4)
#define P210_FFT_CAP_COMPLETION_IRQ         (1U << 5)

#define P210_FFT_ERROR_NONE                 0U
#define P210_FFT_ERROR_BUSY                 1U
#define P210_FFT_ERROR_BAD_LOG2_N           2U
#define P210_FFT_ERROR_BAD_CHANNELS         3U
#define P210_FFT_ERROR_BAD_ALIGNMENT        4U
#define P210_FFT_ERROR_BAD_LENGTH           5U
#define P210_FFT_ERROR_ADDRESS_RANGE        6U
#define P210_FFT_ERROR_ALLOCATION           7U
#define P210_FFT_ERROR_DMA_READ             8U
#define P210_FFT_ERROR_DMA_WRITE            9U
#define P210_FFT_ERROR_BUFFER_OVERLAP       10U

#define P210_FFT_MIN_LOG2_N                 10U
#define P210_FFT_MAX_LOG2_N                 16U
#define P210_FFT_MAX_CHANNELS               2U

#endif
