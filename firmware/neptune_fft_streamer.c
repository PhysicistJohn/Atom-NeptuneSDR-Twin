/*
 * NeptuneSDR P210 guest FFT capture and spectrum streamer.
 *
 * This program runs inside the ARM guest.  It reads a real Linux IIO buffer
 * produced by the ADI cf_axi_adc/axi_dmac drivers, CPU-copies the completed
 * IQ16 block into FFT-visible reserved DDR, starts the P210 PL FFT ABI,
 * converts uint32 linear power into NSFT-v1 log-power packets, and serves
 * those packets over TCP.  This is an IIO-DMAC then CPU-copy path, not a
 * zero-copy PL stream.  The program is intentionally self-contained so Zig
 * can produce a static ARM EABI executable for the released Pluto userspace.
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#define FFT_PHYS_BASE              UINT32_C(0x7c450000)
#define FFT_MMIO_BYTES             UINT32_C(0x1000)
#define FFT_INPUT_PHYS             UINT32_C(0x18000000)
#define FFT_OUTPUT_PHYS            UINT32_C(0x18100000)
#define FFT_LOG2_N                 16U
#define FFT_N                      (UINT32_C(1) << FFT_LOG2_N)
#define FFT_CHANNELS               2U
#define FFT_CHANNEL_MASK           UINT32_C(0x3)
#define FFT_INPUT_BYTES            (FFT_N * FFT_CHANNELS * 4U)
#define FFT_OUTPUT_BYTES           (FFT_N * FFT_CHANNELS * 4U)

#define FFT_REG_ID                 0x000U
#define FFT_REG_VERSION            0x004U
#define FFT_REG_CAPABILITIES       0x008U
#define FFT_REG_CONTROL            0x00cU
#define FFT_REG_STATUS             0x010U
#define FFT_REG_ERROR_CODE         0x014U
#define FFT_REG_LOG2_N             0x018U
#define FFT_REG_CHANNEL_COUNT      0x01cU
#define FFT_REG_CHANNEL_MASK       0x020U
#define FFT_REG_INPUT_ADDR         0x024U
#define FFT_REG_INPUT_BYTES        0x028U
#define FFT_REG_OUTPUT_ADDR        0x02cU
#define FFT_REG_OUTPUT_BYTES       0x030U
#define FFT_REG_SEQUENCE           0x034U
#define FFT_REG_RESULT_SEQUENCE    0x038U
#define FFT_REG_BINS_WRITTEN       0x04cU
#define FFT_REG_MIN_LOG2_N          0x050U
#define FFT_REG_MAX_LOG2_N          0x054U

#define FFT_ID                     UINT32_C(0x5446464e)
#define FFT_ABI_MAJOR              UINT32_C(0x00010000)
#define FFT_CAP_IQ16_LE            (UINT32_C(1) << 0)
#define FFT_CAP_POWER_U32_LE       (UINT32_C(1) << 1)
#define FFT_CAP_TWO_CHANNEL        (UINT32_C(1) << 2)
#define FFT_CAP_SCALE_EACH_STAGE   (UINT32_C(1) << 3)
#define FFT_CAP_NATURAL_ORDER      (UINT32_C(1) << 4)
#define FFT_CAPABILITIES_REQUIRED  (FFT_CAP_IQ16_LE | \
                                    FFT_CAP_POWER_U32_LE | \
                                    FFT_CAP_TWO_CHANNEL | \
                                    FFT_CAP_SCALE_EACH_STAGE | \
                                    FFT_CAP_NATURAL_ORDER)
#define FFT_CONTROL_START          (UINT32_C(1) << 0)
#define FFT_STATUS_BUSY            (UINT32_C(1) << 0)
#define FFT_STATUS_DONE            (UINT32_C(1) << 1)
#define FFT_STATUS_ERROR           (UINT32_C(1) << 2)

#define SAMPLE_RATE_HZ             UINT32_C(61440000)
#define RF_BANDWIDTH_HZ            "50000000\n"
#define SAMPLE_RATE_TEXT           "61440000\n"
#define CENTER_FREQUENCY_HZ        UINT64_C(2400000000)
#define STREAM_PORT                30432U
#define NSFT_HEADER_BYTES          68U
#define NSFT_CRC_BYTES             4U
#define NSFT_ENCODING_UINT16_LOG   2U
#define NSFT_PACKET_VERSION        1U
#define NSFT_DB_FLOOR              (-200.0)
#define NSFT_DB_STEP               0.01
#define NSFT_PACKET_BYTES          (NSFT_HEADER_BYTES + FFT_N * 2U + NSFT_CRC_BYTES)
#define AD9361_ADC_FULL_SCALE       2048.0

static volatile uint32_t *fft_regs;
static uint8_t *fft_input;
static uint32_t *fft_output;
static char adc_sysfs[160];
static char phy_sysfs[160];
static char adc_device[64];

static void fail(const char *what)
{
    int saved = errno;

    fprintf(stderr, "NEPTUNE_FFT fatal=%s errno=%d (%s)\n",
            what, saved, strerror(saved));
    exit(1);
}

static void put_be16(uint8_t *destination, uint16_t value)
{
    destination[0] = (uint8_t)(value >> 8);
    destination[1] = (uint8_t)value;
}

static void put_be32(uint8_t *destination, uint32_t value)
{
    destination[0] = (uint8_t)(value >> 24);
    destination[1] = (uint8_t)(value >> 16);
    destination[2] = (uint8_t)(value >> 8);
    destination[3] = (uint8_t)value;
}

static void put_be64(uint8_t *destination, uint64_t value)
{
    put_be32(destination, (uint32_t)(value >> 32));
    put_be32(destination + 4, (uint32_t)value);
}

static uint32_t crc32_update(uint32_t crc, const uint8_t *bytes, size_t count)
{
    size_t index;

    for (index = 0; index < count; index++) {
        unsigned int bit;

        crc ^= bytes[index];
        for (bit = 0; bit < 8; bit++) {
            uint32_t mask = 0U - (crc & 1U);
            crc = (crc >> 1) ^ (UINT32_C(0xedb88320) & mask);
        }
    }
    return crc;
}

static uint64_t monotonic_nanoseconds(void)
{
    struct timespec now;

    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        fail("clock_gettime");
    }
    return (uint64_t)now.tv_sec * UINT64_C(1000000000) +
           (uint64_t)now.tv_nsec;
}

static int read_name(const char *path, char *value, size_t capacity)
{
    int fd = open(path, O_RDONLY);
    ssize_t count;

    if (fd < 0) {
        return -1;
    }
    count = read(fd, value, capacity - 1);
    close(fd);
    if (count <= 0) {
        return -1;
    }
    while (count > 0 && (value[count - 1] == '\n' || value[count - 1] == '\r')) {
        count--;
    }
    value[count] = '\0';
    return 0;
}

static int find_iio_device(const char *wanted, char *sysfs, size_t sysfs_size,
                           char *device, size_t device_size)
{
    unsigned int index;

    for (index = 0; index < 32; index++) {
        char path[192];
        char name[96];

        snprintf(path, sizeof(path),
                 "/sys/bus/iio/devices/iio:device%u/name", index);
        if (read_name(path, name, sizeof(name)) == 0 && !strcmp(name, wanted)) {
            snprintf(sysfs, sysfs_size,
                     "/sys/bus/iio/devices/iio:device%u", index);
            if (device && device_size) {
                snprintf(device, device_size, "/dev/iio:device%u", index);
            }
            return 0;
        }
    }
    errno = ENODEV;
    return -1;
}

static void write_all_fd(int fd, const void *source, size_t count,
                         const char *what)
{
    const uint8_t *bytes = source;

    while (count) {
        ssize_t written = write(fd, bytes, count);

        if (written < 0 && errno == EINTR) {
            continue;
        }
        if (written <= 0) {
            fail(what);
        }
        bytes += written;
        count -= (size_t)written;
    }
}

static void write_text(const char *path, const char *text)
{
    int fd = open(path, O_WRONLY);

    if (fd < 0) {
        fail(path);
    }
    write_all_fd(fd, text, strlen(text), path);
    if (close(fd) != 0) {
        fail(path);
    }
}

static void write_relative(const char *base, const char *relative,
                           const char *text)
{
    char path[256];

    snprintf(path, sizeof(path), "%s/%s", base, relative);
    write_text(path, text);
}

static void configure_wideband(void)
{
    /* These AD9361 IIO attributes are shared-by-type.  Libiio presents them
     * on both voltage channels, but Linux creates one unindexed sysfs file. */
    write_relative(phy_sysfs, "in_voltage_sampling_frequency", SAMPLE_RATE_TEXT);
    write_relative(phy_sysfs, "in_voltage_rf_bandwidth", RF_BANDWIDTH_HZ);
    fprintf(stderr,
            "NEPTUNE_FFT rf-bandwidth=50000000 sample-rate=61440000\n");
}

static void capture_iio_frame(void)
{
    int fd;
    size_t received = 0;
    unsigned int channel;

    write_relative(adc_sysfs, "buffer/enable", "0\n");
    for (channel = 0; channel < 4; channel++) {
        char relative[80];

        snprintf(relative, sizeof(relative),
                 "scan_elements/in_voltage%u_en", channel);
        write_relative(adc_sysfs, relative, "1\n");
    }
    write_relative(adc_sysfs, "buffer/length", "65536\n");

    /* read(2) copies the completed kernel IIO-DMAC block into the /dev/mem
     * mapping at fft_input.  Physical hardware needs an explicit coherent
     * DMA/streaming design before this can become a zero-copy PL path. */
    fd = open(adc_device, O_RDONLY);
    if (fd < 0) {
        fail(adc_device);
    }
    write_relative(adc_sysfs, "buffer/enable", "1\n");
    while (received < FFT_INPUT_BYTES) {
        ssize_t count = read(fd, fft_input + received, FFT_INPUT_BYTES - received);

        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            write_relative(adc_sysfs, "buffer/enable", "0\n");
            close(fd);
            fail("IIO RX read");
        }
        received += (size_t)count;
    }
    write_relative(adc_sysfs, "buffer/enable", "0\n");
    if (close(fd) != 0) {
        fail(adc_device);
    }
}

static void fft_write(unsigned int offset, uint32_t value)
{
    fft_regs[offset / sizeof(uint32_t)] = value;
    __sync_synchronize();
}

static uint32_t fft_read(unsigned int offset)
{
    uint32_t value;

    __sync_synchronize();
    value = fft_regs[offset / sizeof(uint32_t)];
    __sync_synchronize();
    return value;
}

static void run_fft(uint32_t sequence)
{
    uint32_t status;
    uint64_t deadline;

    fft_write(FFT_REG_STATUS, FFT_STATUS_DONE | FFT_STATUS_ERROR);
    fft_write(FFT_REG_LOG2_N, FFT_LOG2_N);
    fft_write(FFT_REG_CHANNEL_COUNT, FFT_CHANNELS);
    fft_write(FFT_REG_CHANNEL_MASK, FFT_CHANNEL_MASK);
    fft_write(FFT_REG_INPUT_ADDR, FFT_INPUT_PHYS);
    fft_write(FFT_REG_INPUT_BYTES, FFT_INPUT_BYTES);
    fft_write(FFT_REG_OUTPUT_ADDR, FFT_OUTPUT_PHYS);
    fft_write(FFT_REG_OUTPUT_BYTES, FFT_OUTPUT_BYTES);
    fft_write(FFT_REG_SEQUENCE, sequence);
    fft_write(FFT_REG_CONTROL, FFT_CONTROL_START);

    deadline = monotonic_nanoseconds() + UINT64_C(5000000000);
    do {
        status = fft_read(FFT_REG_STATUS);
        if (status & FFT_STATUS_ERROR) {
            fprintf(stderr, "NEPTUNE_FFT accelerator-error=%u\n",
                    fft_read(FFT_REG_ERROR_CODE));
            exit(1);
        }
        if (status & FFT_STATUS_DONE) {
            break;
        }
    } while (monotonic_nanoseconds() < deadline);

    if (!(status & FFT_STATUS_DONE) || (status & FFT_STATUS_BUSY)) {
        errno = ETIMEDOUT;
        fail("FFT completion");
    }
    if (fft_read(FFT_REG_RESULT_SEQUENCE) != sequence ||
        fft_read(FFT_REG_BINS_WRITTEN) != FFT_N * FFT_CHANNELS) {
        errno = EPROTO;
        fail("FFT result metadata");
    }
}

static uint16_t encode_log_power(uint32_t power)
{
    double dbfs;
    double scaled;

    if (!power) {
        return 0;
    }
    /* AD9361 RX samples are signed 12-bit values carried in int16 slots.
     * The scaled FFT preserves a bin-centred tone's input amplitude, so
     * 2048 counts, not the int16 container range, is the 0 dBFS reference. */
    dbfs = 10.0 * log10((double)power /
                        (AD9361_ADC_FULL_SCALE * AD9361_ADC_FULL_SCALE));
    scaled = (dbfs - NSFT_DB_FLOOR) / NSFT_DB_STEP;
    if (scaled <= 0.0) {
        return 0;
    }
    if (scaled >= 65535.0) {
        return UINT16_MAX;
    }
    return (uint16_t)floor(scaled + 0.5);
}

static size_t build_nsft_packet(uint8_t *packet, uint32_t sequence,
                                unsigned int channel, uint64_t timestamp_ns)
{
    const uint32_t *powers = fft_output + channel * FFT_N;
    uint8_t *payload = packet + NSFT_HEADER_BYTES;
    uint32_t payload_bytes = FFT_N * 2U;
    uint32_t crc;
    uint32_t bin;

    memcpy(packet, "NSFT", 4);
    packet[4] = NSFT_PACKET_VERSION;
    packet[5] = NSFT_ENCODING_UINT16_LOG;
    packet[6] = (uint8_t)channel;
    packet[7] = 0;
    put_be64(packet + 8, sequence);
    put_be32(packet + 16, FFT_N);
    put_be32(packet + 20, SAMPLE_RATE_HZ);
    put_be64(packet + 24, CENTER_FREQUENCY_HZ);
    put_be64(packet + 32, timestamp_ns);
    put_be32(packet + 40, 0);             /* configuration epoch */
    put_be32(packet + 44, 0);             /* first natural-order bin */
    put_be32(packet + 48, FFT_N);
    put_be32(packet + 52, 0);             /* dropped frames */
    put_be32(packet + 56, 0);             /* input overruns */
    put_be32(packet + 60, 0);             /* dropped updates */
    put_be32(packet + 64, payload_bytes);

    for (bin = 0; bin < FFT_N; bin++) {
        put_be16(payload + bin * 2U, encode_log_power(powers[bin]));
    }
    crc = crc32_update(UINT32_MAX, packet, NSFT_HEADER_BYTES + payload_bytes) ^
          UINT32_MAX;
    put_be32(packet + NSFT_HEADER_BYTES + payload_bytes, crc);
    return NSFT_HEADER_BYTES + payload_bytes + NSFT_CRC_BYTES;
}

static int send_all_socket(int socket_fd, const uint8_t *bytes, size_t count)
{
    while (count) {
        ssize_t sent = send(socket_fd, bytes, count, MSG_NOSIGNAL);

        if (sent < 0 && errno == EINTR) {
            continue;
        }
        if (sent < 0) {
            return -1;
        }
        if (sent == 0) {
            errno = EPIPE;
            return -1;
        }
        bytes += sent;
        count -= (size_t)sent;
    }
    return 0;
}

static int socket_peer_closed(int socket_fd)
{
    uint8_t byte;
    ssize_t received;

    do {
        received = recv(socket_fd, &byte, sizeof(byte),
                        MSG_PEEK | MSG_DONTWAIT);
    } while (received < 0 && errno == EINTR);

    if (received == 0) {
        return 1;
    }
    if (received < 0 && errno != EAGAIN && errno != EWOULDBLOCK) {
        return 1;
    }
    return 0;
}

static int create_listener(void)
{
    struct sockaddr_in address;
    int listener;
    int one = 1;

    listener = socket(AF_INET, SOCK_STREAM, 0);
    if (listener < 0) {
        fail("socket");
    }
    if (setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one)) != 0) {
        fail("setsockopt");
    }
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_port = htons(STREAM_PORT);
    address.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(listener, (struct sockaddr *)&address, sizeof(address)) != 0) {
        fail("bind");
    }
    if (listen(listener, 2) != 0) {
        fail("listen");
    }
    return listener;
}

static void map_hardware(void)
{
    int fd = open("/dev/mem", O_RDWR | O_SYNC);
    void *mapping;
    uint32_t capabilities;
    uint32_t min_log2_n;
    uint32_t max_log2_n;

    if (fd < 0) {
        fail("/dev/mem");
    }
    mapping = mmap(NULL, FFT_MMIO_BYTES, PROT_READ | PROT_WRITE, MAP_SHARED,
                   fd, FFT_PHYS_BASE);
    if (mapping == MAP_FAILED) {
        fail("FFT MMIO mmap");
    }
    fft_regs = mapping;
    mapping = mmap(NULL, FFT_INPUT_BYTES, PROT_READ | PROT_WRITE, MAP_SHARED,
                   fd, FFT_INPUT_PHYS);
    if (mapping == MAP_FAILED) {
        fail("FFT input mmap");
    }
    fft_input = mapping;
    mapping = mmap(NULL, FFT_OUTPUT_BYTES, PROT_READ | PROT_WRITE, MAP_SHARED,
                   fd, FFT_OUTPUT_PHYS);
    if (mapping == MAP_FAILED) {
        fail("FFT output mmap");
    }
    fft_output = mapping;
    close(fd);

    capabilities = fft_read(FFT_REG_CAPABILITIES);
    min_log2_n = fft_read(FFT_REG_MIN_LOG2_N);
    max_log2_n = fft_read(FFT_REG_MAX_LOG2_N);
    if (fft_read(FFT_REG_ID) != FFT_ID ||
        (fft_read(FFT_REG_VERSION) & UINT32_C(0xffff0000)) != FFT_ABI_MAJOR ||
        (capabilities & FFT_CAPABILITIES_REQUIRED) !=
            FFT_CAPABILITIES_REQUIRED ||
        min_log2_n > FFT_LOG2_N || max_log2_n < FFT_LOG2_N) {
        errno = ENODEV;
        fail("P210 FFT ABI identity");
    }
    fprintf(stderr,
            "NEPTUNE_FFT accelerator-id=%08x version=%08x caps=%08x min-log2=%u max-log2=%u\n",
            fft_read(FFT_REG_ID), fft_read(FFT_REG_VERSION),
            capabilities, min_log2_n, max_log2_n);
}

int main(void)
{
    uint8_t *packet;
    uint32_t sequence = 0;
    int listener;

    setvbuf(stderr, NULL, _IONBF, 0);
    if (find_iio_device("ad9361-phy", phy_sysfs, sizeof(phy_sysfs), NULL, 0) != 0) {
        fail("find ad9361-phy");
    }
    if (find_iio_device("cf-ad9361-lpc", adc_sysfs, sizeof(adc_sysfs),
                        adc_device, sizeof(adc_device)) != 0) {
        fail("find cf-ad9361-lpc");
    }
    map_hardware();
    configure_wideband();
    packet = malloc(NSFT_PACKET_BYTES);
    if (!packet) {
        fail("NSFT packet allocation");
    }
    listener = create_listener();
    fprintf(stderr,
            "NEPTUNE_FFT ready port=30432 n=65536 channels=2 input=iio-dmac-cpu-copy\n");

    for (;;) {
        int client = accept(listener, NULL, NULL);

        if (client < 0 && errno == EINTR) {
            continue;
        }
        if (client < 0) {
            fail("accept");
        }
        fprintf(stderr, "NEPTUNE_FFT client=connected\n");
        for (;;) {
            uint64_t timestamp;
            unsigned int channel;
            int send_error = 0;

            sequence++;
            capture_iio_frame();
            run_fft(sequence);
            timestamp = monotonic_nanoseconds();
            for (channel = 0; channel < FFT_CHANNELS; channel++) {
                size_t count = build_nsft_packet(packet, sequence, channel,
                                                 timestamp);
                if (send_all_socket(client, packet, count) != 0) {
                    send_error = errno ? errno : EPIPE;
                    break;
                }
            }
            if (send_error) {
                fprintf(stderr,
                        "NEPTUNE_FFT client=send-failed errno=%d (%s)\n",
                        send_error, strerror(send_error));
                break;
            }
            fprintf(stderr,
                    "NEPTUNE_FFT transmitted sequence=%u bins=%u bytes=%u\n",
                    sequence, FFT_N * FFT_CHANNELS,
                    2U * NSFT_PACKET_BYTES);

            /* Pacing only: this delay is not a sustained-rate or 20 Hz claim. */
            {
                struct timespec interval = { .tv_sec = 0, .tv_nsec = 50000000 };
                nanosleep(&interval, NULL);
            }
            if (socket_peer_closed(client)) {
                break;
            }
        }
        shutdown(client, SHUT_RDWR);
        close(client);
        fprintf(stderr, "NEPTUNE_FFT client=disconnected\n");
    }
}
