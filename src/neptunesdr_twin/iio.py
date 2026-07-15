"""libiio context surface and IIOD network-protocol endpoint.

The text/binary protocol is the same interpreter used above Pluto's IP and
native FunctionFS transports.  A stock libiio client can therefore target
``ip:127.0.0.1`` while the twin runs on any host; Linux gadget deployment can
route the same context over the modeled USB IIO interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import errno
from html import escape
import socketserver
import threading
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .ad9361 import AD9361, GainMode
from .errors import TwinError


Getter = Callable[[], str]
Setter = Callable[[str], None]
RxProvider = Callable[[int], bytes]
TxConsumer = Callable[[bytes], None]


@dataclass
class IIOAttribute:
    name: str
    getter: Getter
    setter: Optional[Setter] = None
    filename: Optional[str] = None

    def read(self) -> str:
        return str(self.getter())

    def write(self, value: str) -> None:
        if self.setter is None:
            raise PermissionError("attribute %s is read-only" % self.name)
        self.setter(value)


@dataclass
class IIOChannel:
    id: str
    output: bool
    name: Optional[str] = None
    scan_index: Optional[int] = None
    scan_format: str = "le:S12/16&gt;&gt;0"
    attributes: Dict[str, IIOAttribute] = field(default_factory=dict)

    def xml(self) -> str:
        parts = [
            '<channel id="%s"%s type="%s" >'
            % (
                escape(self.id, quote=True),
                (' name="%s"' % escape(self.name, quote=True)) if self.name else "",
                "output" if self.output else "input",
            )
        ]
        if self.scan_index is not None:
            parts.append(
                '<scan-element index="%d" format="%s" />'
                % (self.scan_index, self.scan_format)
            )
        for attribute in self.attributes.values():
            suffix = (
                ' filename="%s"' % escape(attribute.filename, quote=True)
                if attribute.filename
                else ""
            )
            parts.append('<attribute name="%s"%s />' % (escape(attribute.name), suffix))
        parts.append("</channel>")
        return "".join(parts)


@dataclass
class IIODevice:
    id: str
    name: str
    channels: List[IIOChannel] = field(default_factory=list)
    attributes: Dict[str, IIOAttribute] = field(default_factory=dict)
    debug_attributes: Dict[str, IIOAttribute] = field(default_factory=dict)
    buffer_attributes: Dict[str, IIOAttribute] = field(default_factory=dict)
    rx_provider: Optional[RxProvider] = None
    tx_consumer: Optional[TxConsumer] = None

    @property
    def scan_channels(self) -> Tuple[IIOChannel, ...]:
        return tuple(channel for channel in self.channels if channel.scan_index is not None)

    def channel(self, channel_id: str, output: bool) -> IIOChannel:
        for channel in self.channels:
            if channel.id == channel_id and channel.output == output:
                return channel
        raise KeyError("channel %s" % channel_id)

    def xml(self) -> str:
        parts = ['<device id="%s" name="%s" >' % (escape(self.id), escape(self.name))]
        parts.extend(channel.xml() for channel in self.channels)
        parts.extend('<attribute name="%s" />' % escape(name) for name in self.attributes)
        parts.extend(
            '<buffer-attribute name="%s" />' % escape(name) for name in self.buffer_attributes
        )
        parts.extend(
            '<debug-attribute name="%s" />' % escape(name) for name in self.debug_attributes
        )
        parts.append("</device>")
        return "".join(parts)


XML_DTD = (
    '<!DOCTYPE context ['
    '<!ELEMENT context (device | context-attribute)*>'
    '<!ELEMENT context-attribute EMPTY>'
    '<!ELEMENT device (channel | attribute | debug-attribute | buffer-attribute)*>'
    '<!ELEMENT channel (scan-element?, attribute*)>'
    '<!ELEMENT attribute EMPTY><!ELEMENT scan-element EMPTY>'
    '<!ELEMENT debug-attribute EMPTY><!ELEMENT buffer-attribute EMPTY>'
    '<!ATTLIST context name CDATA #REQUIRED version-major CDATA #REQUIRED '
    'version-minor CDATA #REQUIRED version-git CDATA #REQUIRED description CDATA #IMPLIED>'
    '<!ATTLIST context-attribute name CDATA #REQUIRED value CDATA #REQUIRED>'
    '<!ATTLIST device id CDATA #REQUIRED name CDATA #IMPLIED label CDATA #IMPLIED>'
    '<!ATTLIST channel id CDATA #REQUIRED type (input|output) #REQUIRED name CDATA #IMPLIED>'
    '<!ATTLIST scan-element index CDATA #REQUIRED format CDATA #REQUIRED scale CDATA #IMPLIED>'
    '<!ATTLIST attribute name CDATA #REQUIRED filename CDATA #IMPLIED>'
    '<!ATTLIST debug-attribute name CDATA #REQUIRED>'
    '<!ATTLIST buffer-attribute name CDATA #REQUIRED>'
    ']>'
)


class IIOContext:
    """Observable IIO schema and attribute behavior for a P210-like image."""

    VERSION_MAJOR = 0
    VERSION_MINOR = 26
    VERSION_GIT = "v0.26"

    def __init__(
        self,
        radio: AD9361,
        serial: str = "P210TWIN000000000000000000000001",
        firmware_version: str = "p210-twin-0.1.0",
        rx_provider: Optional[RxProvider] = None,
        tx_consumer: Optional[TxConsumer] = None,
    ) -> None:
        self.radio = radio
        self.serial = serial
        self.firmware_version = firmware_version
        self.context_attributes: Dict[str, str] = {
            "hw_model": "NeptuneSDR P210 (XC7Z020-AD9361) [digital twin]",
            "hw_model_variant": "P210-V2-unverified",
            "hw_serial": serial,
            "fw_version": firmware_version,
            "ad9361-phy,xo_correction": str(radio.reference_clock_hz),
            "uri": "ip:127.0.0.1",
        }
        self.devices: List[IIODevice] = []
        self._build_devices(rx_provider, tx_consumer)

    def find_device(self, identity: str) -> IIODevice:
        for device in self.devices:
            if identity in {device.id, device.name}:
                return device
        raise KeyError("device %s" % identity)

    def xml(self) -> str:
        parts = [
            '<?xml version="1.0" encoding="utf-8"?>',
            XML_DTD,
            '<context name="local" version-major="%d" version-minor="%d" '
            'version-git="%s" description="Linux p210-twin armv7l">'
            % (self.VERSION_MAJOR, self.VERSION_MINOR, self.VERSION_GIT),
        ]
        parts.extend(
            '<context-attribute name="%s" value="%s" />'
            % (escape(name, quote=True), escape(value, quote=True))
            for name, value in self.context_attributes.items()
        )
        parts.extend(device.xml() for device in self.devices)
        parts.append("</context>")
        return "".join(parts)

    def _build_devices(
        self, rx_provider: Optional[RxProvider], tx_consumer: Optional[TxConsumer]
    ) -> None:
        physical = IIODevice("iio:device0", "ad9361-phy")
        for channel_index in range(2):
            rx = IIOChannel("voltage%d" % channel_index, output=False)
            rx.attributes = self._rx_attributes(channel_index)
            physical.channels.append(rx)
            tx = IIOChannel("voltage%d" % channel_index, output=True)
            tx.attributes = self._tx_attributes(channel_index)
            physical.channels.append(tx)
        physical.channels.extend(
            [
                IIOChannel(
                    "altvoltage0",
                    output=True,
                    name="RX_LO",
                    attributes=self._lo_attributes("rx"),
                ),
                IIOChannel(
                    "altvoltage1",
                    output=True,
                    name="TX_LO",
                    attributes=self._lo_attributes("tx"),
                ),
                IIOChannel(
                    "temp0",
                    output=False,
                    attributes={
                        "input": IIOAttribute("input", lambda: "40000"),
                    },
                ),
            ]
        )
        physical.attributes = {
            "ensm_mode": IIOAttribute(
                "ensm_mode", lambda: self.radio.state.name.lower(), self._write_ensm
            ),
            "ensm_mode_available": IIOAttribute(
                "ensm_mode_available", lambda: "sleep alert rx tx fdd"
            ),
            "calib_mode": IIOAttribute("calib_mode", lambda: "auto"),
            "trx_rate_governor": IIOAttribute("trx_rate_governor", lambda: "nominal"),
            "filter_fir_en": IIOAttribute("filter_fir_en", lambda: "0"),
        }
        physical.debug_attributes = {
            "direct_reg_access": IIOAttribute(
                "direct_reg_access", lambda: "0x0", self._write_direct_register
            )
        }
        self.devices.append(physical)

        xadc = IIODevice("iio:device1", "xadc")
        xadc.channels.append(
            IIOChannel(
                "temp0",
                output=False,
                attributes={
                    "raw": IIOAttribute("raw", lambda: "3000"),
                    "scale": IIOAttribute("scale", lambda: "123.040771"),
                    "offset": IIOAttribute("offset", lambda: "-2219"),
                },
            )
        )
        self.devices.append(xadc)

        tx_device = IIODevice(
            "iio:device2", "cf-ad9361-dds-core-lpc", tx_consumer=tx_consumer
        )
        for index in range(4):
            tx_device.channels.append(
                IIOChannel(
                    "voltage%d" % index,
                    output=True,
                    scan_index=index,
                    attributes={
                        "raw": IIOAttribute("raw", lambda: "0"),
                        "scale": IIOAttribute("scale", lambda: "1.000000"),
                    },
                )
            )
        tx_device.buffer_attributes = {
            "data_available": IIOAttribute("data_available", lambda: "0"),
            "direction": IIOAttribute("direction", lambda: "out"),
            "length_align_bytes": IIOAttribute("length_align_bytes", lambda: "8"),
        }
        self.devices.append(tx_device)

        rx_device = IIODevice("iio:device3", "cf-ad9361-lpc", rx_provider=rx_provider)
        for index in range(4):
            rx_device.channels.append(
                IIOChannel(
                    "voltage%d" % index,
                    output=False,
                    scan_index=index,
                    attributes={
                        "raw": IIOAttribute("raw", lambda: "0"),
                        "scale": IIOAttribute("scale", lambda: "1.000000"),
                    },
                )
            )
        rx_device.buffer_attributes = {
            "data_available": IIOAttribute("data_available", lambda: "0"),
            "direction": IIOAttribute("direction", lambda: "in"),
            "length_align_bytes": IIOAttribute("length_align_bytes", lambda: "8"),
        }
        self.devices.append(rx_device)

    def _rx_attributes(self, channel: int) -> Dict[str, IIOAttribute]:
        return {
            "hardwaregain": IIOAttribute(
                "hardwaregain",
                lambda channel=channel: "%.6f" % self.radio.rx_channels[channel].gain_db,
                lambda value, channel=channel: self.radio.set_rx_gain(channel, float(value)),
            ),
            "gain_control_mode": IIOAttribute(
                "gain_control_mode",
                lambda channel=channel: self.radio.rx_channels[channel].gain_mode.value,
                lambda value, channel=channel: self.radio.set_rx_gain_mode(channel, GainMode(value)),
            ),
            "gain_control_mode_available": IIOAttribute(
                "gain_control_mode_available", lambda: "manual slow_attack fast_attack hybrid"
            ),
            "rf_bandwidth": IIOAttribute(
                "rf_bandwidth",
                lambda: str(self.radio.rx_bandwidth_hz),
                lambda value: self.radio.set_rf_bandwidth("rx", int(value)),
            ),
            "rf_bandwidth_available": IIOAttribute(
                "rf_bandwidth_available", lambda: "[200000 1 56000000]"
            ),
            "sampling_frequency": IIOAttribute(
                "sampling_frequency",
                lambda: str(self.radio.sample_rate_hz),
                lambda value: self.radio.set_sample_rate(int(value)),
            ),
            "sampling_frequency_available": IIOAttribute(
                "sampling_frequency_available", lambda: "[520833 1 61440000]"
            ),
            "rf_port_select": IIOAttribute(
                "rf_port_select",
                lambda channel=channel: self.radio.rx_channels[channel].rf_port,
                lambda value, channel=channel: setattr(self.radio.rx_channels[channel], "rf_port", value),
            ),
            "rf_port_select_available": IIOAttribute(
                "rf_port_select_available", lambda: "A_BALANCED B_BALANCED C_BALANCED A_N A_P B_N B_P C_N C_P TX_MONITOR1 TX_MONITOR2 TX_MONITOR1_2"
            ),
        }

    def _tx_attributes(self, channel: int) -> Dict[str, IIOAttribute]:
        return {
            "hardwaregain": IIOAttribute(
                "hardwaregain",
                lambda channel=channel: "%.6f" % -self.radio.tx_channels[channel].attenuation_db,
                lambda value, channel=channel: self.radio.set_tx_attenuation(channel, -float(value)),
            ),
            "rf_bandwidth": IIOAttribute(
                "rf_bandwidth",
                lambda: str(self.radio.tx_bandwidth_hz),
                lambda value: self.radio.set_rf_bandwidth("tx", int(value)),
            ),
            "rf_bandwidth_available": IIOAttribute(
                "rf_bandwidth_available", lambda: "[200000 1 56000000]"
            ),
            "sampling_frequency": IIOAttribute(
                "sampling_frequency",
                lambda: str(self.radio.sample_rate_hz),
                lambda value: self.radio.set_sample_rate(int(value)),
            ),
            "sampling_frequency_available": IIOAttribute(
                "sampling_frequency_available", lambda: "[520833 1 61440000]"
            ),
            "rf_port_select": IIOAttribute(
                "rf_port_select",
                lambda channel=channel: self.radio.tx_channels[channel].rf_port,
                lambda value, channel=channel: setattr(self.radio.tx_channels[channel], "rf_port", value),
            ),
            "rf_port_select_available": IIOAttribute("rf_port_select_available", lambda: "A B"),
        }

    def _lo_attributes(self, direction: str) -> Dict[str, IIOAttribute]:
        return {
            "frequency": IIOAttribute(
                "frequency",
                lambda: str(self.radio.rx_lo_hz if direction == "rx" else self.radio.tx_lo_hz),
                lambda value: self.radio.set_lo_frequency(direction, int(value)),
            ),
            "frequency_available": IIOAttribute(
                "frequency_available", lambda: "[70000000 1 6000000000]"
            ),
            "powerdown": IIOAttribute("powerdown", lambda: "0"),
        }

    def _write_ensm(self, value: str) -> None:
        from .ad9361 import ENSMState

        self.radio.set_ensm_state(ENSMState[value.strip().upper()])

    def _write_direct_register(self, value: str) -> None:
        fields = value.split()
        if len(fields) != 2:
            raise ValueError("direct_reg_access expects '<address> <value>'")
        self.radio.write_register(int(fields[0], 0), int(fields[1], 0))


@dataclass
class _OpenBuffer:
    samples_count: int
    mask: int
    cyclic: bool


class IIODSession:
    """One stateful IIOD RPC session, independent of its byte transport."""

    def __init__(self, context: IIOContext) -> None:
        self.context = context
        self.open_buffers: Dict[str, _OpenBuffer] = {}
        self.timeout_ms = 5000
        self.closed = False

    def execute(self, line: bytes, payload: Optional[bytes] = None) -> bytes:
        try:
            command = line.decode("ascii").strip("\r\n")
        except UnicodeDecodeError:
            return self._integer(-errno.EINVAL)
        if not command:
            return b""
        tokens = command.split()
        verb = tokens[0].upper()
        try:
            if verb == "VERSION" and len(tokens) == 1:
                return b"0.26.v0.26  \n"
            if verb == "PRINT" and len(tokens) == 1:
                xml = self.context.xml().encode("utf-8")
                return self._integer(len(xml)) + xml + b"\n"
            if verb == "ZPRINT":
                return self._integer(-errno.EINVAL)
            if verb == "TIMEOUT" and len(tokens) == 2:
                self.timeout_ms = max(0, int(tokens[1]))
                return self._integer(0)
            if verb == "OPEN" and len(tokens) in (4, 5):
                device = self.context.find_device(tokens[1])
                if not device.scan_channels:
                    return self._integer(-errno.ENODEV)
                if device.id in self.open_buffers:
                    return self._integer(-errno.EBUSY)
                cyclic = len(tokens) == 5 and tokens[4].upper() == "CYCLIC"
                self.open_buffers[device.id] = _OpenBuffer(
                    samples_count=int(tokens[2]), mask=int(tokens[3], 16), cyclic=cyclic
                )
                return self._integer(0)
            if verb == "CLOSE" and len(tokens) == 2:
                device = self.context.find_device(tokens[1])
                if device.id not in self.open_buffers:
                    return self._integer(-errno.EBADF)
                del self.open_buffers[device.id]
                return self._integer(0)
            if verb == "READBUF" and len(tokens) == 3:
                return self._read_buffer(tokens[1], int(tokens[2]))
            if verb == "WRITEBUF" and len(tokens) == 3:
                return self._write_buffer(tokens[1], int(tokens[2]), payload)
            if verb == "READ":
                value = self._attribute(tokens[1:], write=False, payload=None)
                data = value.encode("utf-8")
                return self._integer(len(data)) + data + b"\n"
            if verb == "WRITE":
                if payload is None:
                    return self._integer(-errno.EINVAL)
                value = payload.decode("utf-8").rstrip("\0")
                self._attribute(tokens[1:-1], write=True, payload=value)
                return self._integer(len(payload))
            if verb == "GETTRIG" and len(tokens) == 2:
                self.context.find_device(tokens[1])
                return self._integer(0)
            if verb == "SETTRIG" and len(tokens) in (2, 3):
                self.context.find_device(tokens[1])
                return self._integer(0 if len(tokens) == 2 else -errno.ENODEV)
            if verb == "SET" and len(tokens) == 4 and tokens[2].upper() == "BUFFERS_COUNT":
                self.context.find_device(tokens[1])
                return self._integer(0)
            if verb == "EXIT":
                self.closed = True
                return b""
            if verb == "HELP":
                return b"PRINT VERSION READ WRITE OPEN CLOSE READBUF WRITEBUF TIMEOUT EXIT\n"
            return self._integer(-errno.EINVAL)
        except KeyError:
            return self._integer(-errno.ENODEV)
        except PermissionError:
            return self._integer(-errno.EPERM)
        except (ValueError, TwinError):
            return self._integer(-errno.EINVAL)

    def _attribute(self, tokens: Sequence[str], write: bool, payload: Optional[str]) -> str:
        if not tokens:
            raise ValueError("missing device")
        device = self.context.find_device(tokens[0])
        if len(tokens) >= 2 and tokens[1].upper() in {"INPUT", "OUTPUT"}:
            if len(tokens) != 4:
                raise ValueError("channel attribute command requires direction, channel, attribute")
            channel = device.channel(tokens[2], tokens[1].upper() == "OUTPUT")
            attribute = channel.attributes[tokens[3]]
        elif len(tokens) >= 2 and tokens[1].upper() in {"DEBUG", "BUFFER"}:
            if len(tokens) != 3:
                raise ValueError("typed attribute command requires a name")
            collection = (
                device.debug_attributes if tokens[1].upper() == "DEBUG" else device.buffer_attributes
            )
            attribute = collection[tokens[2]]
        else:
            if len(tokens) != 2:
                raise ValueError("device attribute command requires a name")
            attribute = device.attributes[tokens[1]]
        if write:
            attribute.write(payload or "")
            return ""
        return attribute.read()

    def _read_buffer(self, identity: str, length: int) -> bytes:
        device = self.context.find_device(identity)
        opened = self.open_buffers.get(device.id)
        if opened is None:
            return self._integer(-errno.EBADF)
        if device.rx_provider is None:
            return self._integer(-errno.EPERM)
        if length <= 0:
            return self._integer(-errno.EINVAL)
        data = device.rx_provider(length)
        if len(data) < length:
            data += b"\0" * (length - len(data))
        elif len(data) > length:
            data = data[:length]
        words = max(1, (len(device.channels) + 31) // 32)
        mask = "".join("%08x" % ((opened.mask >> (32 * index)) & 0xFFFFFFFF) for index in reversed(range(words)))
        return self._integer(len(data)) + mask.encode("ascii") + b"\n" + data

    def _write_buffer(self, identity: str, length: int, payload: Optional[bytes]) -> bytes:
        device = self.context.find_device(identity)
        if device.id not in self.open_buffers:
            return self._integer(-errno.EBADF)
        if device.tx_consumer is None:
            return self._integer(-errno.EPERM)
        if payload is None:
            return self._integer(length)  # First WRITEBUF handshake.
        if len(payload) != length:
            return self._integer(-errno.EIO)
        device.tx_consumer(payload)
        return self._integer(length)

    @staticmethod
    def _integer(value: int) -> bytes:
        return ("%d\n" % int(value)).encode("ascii")


class _IIODRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        session = IIODSession(self.server.context)  # type: ignore[attr-defined]
        while not session.closed:
            line = self.rfile.readline(64 * 1024)
            if not line:
                break
            tokens = line.decode("ascii", errors="ignore").strip().split()
            payload: Optional[bytes] = None
            if tokens and tokens[0].upper() == "WRITE" and tokens[-1].isdigit():
                payload = self.rfile.read(int(tokens[-1]))
            elif tokens and tokens[0].upper() == "WRITEBUF" and len(tokens) == 3:
                length = int(tokens[2])
                first = session.execute(line, None)
                self.wfile.write(first)
                self.wfile.flush()
                if first.startswith(b"-"):
                    continue
                payload = self.rfile.read(length)
            response = session.execute(line, payload)
            if response:
                self.wfile.write(response)
                self.wfile.flush()


class IIODServer(socketserver.ThreadingTCPServer):
    """Threaded IIOD TCP endpoint; defaults to the standard port 30431."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, context: IIOContext, host: str = "127.0.0.1", port: int = 30431):
        self.context = context
        super().__init__((host, int(port)), _IIODRequestHandler)
        self._thread: Optional[threading.Thread] = None

    @property
    def address(self) -> Tuple[str, int]:
        host, port = self.server_address
        return str(host), int(port)

    def start(self) -> "IIODServer":
        if self._thread is not None:
            raise RuntimeError("IIOD server already started")
        self._thread = threading.Thread(target=self.serve_forever, name="p210-iiod", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread is not None:
            self.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self.server_close()

    def __enter__(self) -> "IIODServer":
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop()

