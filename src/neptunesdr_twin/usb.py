"""USB descriptor and EP0 model for the observed NeptuneSDR/P210 gadget.

The normal personality is the Linux composite gadget used by PlutoSDR
firmware: RNDIS, mass storage, CDC ACM, and native libiio FunctionFS.  The
descriptor bytes live in :mod:`neptunesdr_twin.data` so captures can be
reviewed independently of this parser.

This module deliberately models control-plane behaviour only.  Bulk endpoint
payloads belong to the network, storage, console, and IIO subsystem models;
the native-IIO vendor requests here only manage the three FunctionFS pipes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
import struct
from typing import Mapping, Optional, Sequence, Set, Tuple, Union

from .errors import USBProtocolError


USB_DIR_OUT = 0x00
USB_DIR_IN = 0x80

USB_TYPE_STANDARD = 0x00
USB_TYPE_VENDOR = 0x40
USB_RECIP_DEVICE = 0x00
USB_RECIP_INTERFACE = 0x01

USB_REQ_GET_CONFIGURATION = 0x08
USB_REQ_SET_CONFIGURATION = 0x09
USB_REQ_GET_DESCRIPTOR = 0x06

USB_DT_DEVICE = 0x01
USB_DT_CONFIG = 0x02
USB_DT_STRING = 0x03
USB_DT_INTERFACE = 0x04
USB_DT_ENDPOINT = 0x05
USB_DT_INTERFACE_ASSOCIATION = 0x0B

IIO_REQ_RESET_PIPES = 0
IIO_REQ_OPEN_PIPE = 1
IIO_REQ_CLOSE_PIPE = 2

NORMAL_VENDOR_ID = 0x0456
NORMAL_PRODUCT_ID = 0xB673
DFU_PRODUCT_ID = 0xB674
IIO_INTERFACE_NUMBER = 5
IIO_PIPE_COUNT = 3


def _bounded_integer(name: str, value: int, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("%s must be an integer in [0, %d]" % (name, maximum))
    return value


@dataclass(frozen=True)
class SetupPacket:
    """The fixed eight-byte USB setup packet."""

    request_type: int
    request: int
    value: int = 0
    index: int = 0
    length: int = 0

    def __post_init__(self) -> None:
        _bounded_integer("request_type", self.request_type, 0xFF)
        _bounded_integer("request", self.request, 0xFF)
        _bounded_integer("value", self.value, 0xFFFF)
        _bounded_integer("index", self.index, 0xFFFF)
        _bounded_integer("length", self.length, 0xFFFF)

    @property
    def direction_in(self) -> bool:
        return bool(self.request_type & USB_DIR_IN)

    @property
    def descriptor_type(self) -> int:
        return (self.value >> 8) & 0xFF

    @property
    def descriptor_index(self) -> int:
        return self.value & 0xFF

    def to_bytes(self) -> bytes:
        return struct.pack(
            "<BBHHH",
            self.request_type,
            self.request,
            self.value,
            self.index,
            self.length,
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "SetupPacket":
        if len(data) != 8:
            raise USBProtocolError("a USB setup packet is exactly eight bytes")
        return cls(*struct.unpack("<BBHHH", data))


@dataclass(frozen=True)
class Descriptor:
    """One bounds-checked descriptor from a descriptor stream."""

    offset: int
    length: int
    descriptor_type: int
    raw: bytes

    @property
    def b_length(self) -> int:
        return self.length

    @property
    def b_descriptor_type(self) -> int:
        return self.descriptor_type


@dataclass(frozen=True)
class DeviceDescriptor:
    usb_version: int
    device_class: int
    device_subclass: int
    device_protocol: int
    max_packet_size_ep0: int
    vendor_id: int
    product_id: int
    device_version: int
    manufacturer_index: int
    product_index: int
    serial_number_index: int
    configuration_count: int
    raw: bytes

    @property
    def bcd_usb(self) -> int:
        return self.usb_version

    @property
    def bcd_device(self) -> int:
        return self.device_version


@dataclass(frozen=True)
class EndpointDescriptor:
    address: int
    attributes: int
    max_packet_size: int
    interval: int
    descriptor: Descriptor

    @property
    def direction(self) -> str:
        return "in" if self.address & USB_DIR_IN else "out"

    @property
    def endpoint_number(self) -> int:
        return self.address & 0x0F

    @property
    def transfer_type(self) -> int:
        return self.attributes & 0x03


@dataclass(frozen=True)
class InterfaceDescriptor:
    number: int
    alternate_setting: int
    declared_endpoint_count: int
    interface_class: int
    interface_subclass: int
    interface_protocol: int
    string_index: int
    descriptor: Descriptor
    endpoints: Tuple[EndpointDescriptor, ...] = ()
    extra_descriptors: Tuple[Descriptor, ...] = ()

    @property
    def endpoint_addresses(self) -> Tuple[int, ...]:
        return tuple(endpoint.address for endpoint in self.endpoints)


@dataclass(frozen=True)
class InterfaceAssociationDescriptor:
    first_interface: int
    interface_count: int
    function_class: int
    function_subclass: int
    function_protocol: int
    string_index: int
    descriptor: Descriptor


@dataclass(frozen=True)
class ConfigurationDescriptor:
    total_length: int
    declared_interface_count: int
    configuration_value: int
    string_index: int
    attributes: int
    max_power_units: int
    descriptors: Tuple[Descriptor, ...]
    interfaces: Tuple[InterfaceDescriptor, ...]
    associations: Tuple[InterfaceAssociationDescriptor, ...]
    unowned_descriptors: Tuple[Descriptor, ...]
    raw: bytes

    @property
    def max_power_ma(self) -> int:
        return self.max_power_units * 2

    @property
    def endpoint_addresses(self) -> Tuple[int, ...]:
        result = []
        for interface in self.interfaces:
            result.extend(interface.endpoint_addresses)
        return tuple(result)

    def interface(self, number: int, alternate_setting: int = 0) -> InterfaceDescriptor:
        for interface in self.interfaces:
            if (
                interface.number == number
                and interface.alternate_setting == alternate_setting
            ):
                return interface
        raise KeyError((number, alternate_setting))


def parse_descriptor_stream(
    data: bytes, start: int = 0, end: Optional[int] = None
) -> Tuple[Descriptor, ...]:
    """Split a USB descriptor stream while enforcing every length boundary."""

    blob = bytes(data)
    limit = len(blob) if end is None else end
    if start < 0 or limit < start or limit > len(blob):
        raise USBProtocolError("descriptor stream bounds are invalid")
    descriptors = []
    position = start
    while position < limit:
        if limit - position < 2:
            raise USBProtocolError("truncated descriptor header at offset %d" % position)
        length = blob[position]
        descriptor_type = blob[position + 1]
        if length < 2:
            raise USBProtocolError("descriptor at offset %d has bLength %d" % (position, length))
        descriptor_end = position + length
        if descriptor_end > limit:
            raise USBProtocolError("descriptor at offset %d exceeds its stream" % position)
        descriptors.append(
            Descriptor(position, length, descriptor_type, blob[position:descriptor_end])
        )
        position = descriptor_end
    return tuple(descriptors)


def parse_device_descriptor(data: bytes) -> DeviceDescriptor:
    """Parse the standard, 18-byte USB device descriptor."""

    raw = bytes(data)
    if len(raw) != 18:
        raise USBProtocolError("device descriptor must be exactly 18 bytes")
    if raw[0] != 18 or raw[1] != USB_DT_DEVICE:
        raise USBProtocolError("invalid USB device descriptor header")
    fields = struct.unpack("<BBHBBBBHHHBBBB", raw)
    return DeviceDescriptor(
        usb_version=fields[2],
        device_class=fields[3],
        device_subclass=fields[4],
        device_protocol=fields[5],
        max_packet_size_ep0=fields[6],
        vendor_id=fields[7],
        product_id=fields[8],
        device_version=fields[9],
        manufacturer_index=fields[10],
        product_index=fields[11],
        serial_number_index=fields[12],
        configuration_count=fields[13],
        raw=raw,
    )


def _endpoint_from_descriptor(descriptor: Descriptor) -> EndpointDescriptor:
    if descriptor.length != 7:
        raise USBProtocolError(
            "endpoint descriptor at offset %d must be seven bytes" % descriptor.offset
        )
    _, _, address, attributes, maximum, interval = struct.unpack(
        "<BBBBHB", descriptor.raw
    )
    return EndpointDescriptor(address, attributes, maximum, interval, descriptor)


def _association_from_descriptor(
    descriptor: Descriptor,
) -> InterfaceAssociationDescriptor:
    if descriptor.length != 8:
        raise USBProtocolError(
            "interface association at offset %d must be eight bytes" % descriptor.offset
        )
    values = struct.unpack("<BBBBBBBB", descriptor.raw)
    return InterfaceAssociationDescriptor(
        first_interface=values[2],
        interface_count=values[3],
        function_class=values[4],
        function_subclass=values[5],
        function_protocol=values[6],
        string_index=values[7],
        descriptor=descriptor,
    )


def parse_configuration_descriptor(data: bytes) -> ConfigurationDescriptor:
    """Parse a complete configuration descriptor, including all children."""

    raw = bytes(data)
    if len(raw) < 9:
        raise USBProtocolError("configuration descriptor is shorter than its header")
    if raw[0] != 9 or raw[1] != USB_DT_CONFIG:
        raise USBProtocolError("invalid USB configuration descriptor header")
    total_length = struct.unpack_from("<H", raw, 2)[0]
    if total_length != len(raw):
        raise USBProtocolError(
            "configuration wTotalLength %d does not match %d bytes"
            % (total_length, len(raw))
        )

    descriptors = parse_descriptor_stream(raw, 9, total_length)
    associations = []
    unowned = []
    interface_builders = []
    current = None
    for descriptor in descriptors:
        if descriptor.descriptor_type == USB_DT_INTERFACE_ASSOCIATION:
            associations.append(_association_from_descriptor(descriptor))
            current = None
            continue
        if descriptor.descriptor_type == USB_DT_INTERFACE:
            if descriptor.length != 9:
                raise USBProtocolError(
                    "interface descriptor at offset %d must be nine bytes"
                    % descriptor.offset
                )
            values = struct.unpack("<BBBBBBBBB", descriptor.raw)
            current = {
                "values": values,
                "descriptor": descriptor,
                "endpoints": [],
                "extra": [],
            }
            interface_builders.append(current)
            continue
        if descriptor.descriptor_type == USB_DT_ENDPOINT:
            endpoint = _endpoint_from_descriptor(descriptor)
            if current is None:
                unowned.append(descriptor)
            else:
                current["endpoints"].append(endpoint)
            continue
        if current is None:
            unowned.append(descriptor)
        else:
            current["extra"].append(descriptor)

    interfaces = []
    for builder in interface_builders:
        values = builder["values"]
        interfaces.append(
            InterfaceDescriptor(
                number=values[2],
                alternate_setting=values[3],
                declared_endpoint_count=values[4],
                interface_class=values[5],
                interface_subclass=values[6],
                interface_protocol=values[7],
                string_index=values[8],
                descriptor=builder["descriptor"],
                endpoints=tuple(builder["endpoints"]),
                extra_descriptors=tuple(builder["extra"]),
            )
        )

    return ConfigurationDescriptor(
        total_length=total_length,
        declared_interface_count=raw[4],
        configuration_value=raw[5],
        string_index=raw[6],
        attributes=raw[7],
        max_power_units=raw[8],
        descriptors=descriptors,
        interfaces=tuple(interfaces),
        associations=tuple(associations),
        unowned_descriptors=tuple(unowned),
        raw=raw,
    )


def configuration_descriptor_issues(
    configuration: ConfigurationDescriptor,
) -> Tuple[str, ...]:
    """Return logical consistency problems not covered by byte bounds checks."""

    issues = []
    if not configuration.configuration_value:
        issues.append("bConfigurationValue must be non-zero")
    if not configuration.attributes & 0x80:
        issues.append("configuration bmAttributes bit 7 must be set")

    identities = [
        (interface.number, interface.alternate_setting)
        for interface in configuration.interfaces
    ]
    if len(identities) != len(set(identities)):
        issues.append("interface number/alternate-setting pairs are not unique")
    interface_numbers = {interface.number for interface in configuration.interfaces}
    expected_numbers = set(range(configuration.declared_interface_count))
    if interface_numbers != expected_numbers:
        issues.append(
            "bNumInterfaces declares %d but interface numbers are %s"
            % (
                configuration.declared_interface_count,
                sorted(interface_numbers),
            )
        )

    active_endpoint_addresses = []
    for interface in configuration.interfaces:
        if len(interface.endpoints) != interface.declared_endpoint_count:
            issues.append(
                "interface %d alt %d declares %d endpoints but contains %d"
                % (
                    interface.number,
                    interface.alternate_setting,
                    interface.declared_endpoint_count,
                    len(interface.endpoints),
                )
            )
        for endpoint in interface.endpoints:
            if endpoint.address & 0x70 or endpoint.endpoint_number == 0:
                issues.append("invalid endpoint address 0x%02x" % endpoint.address)
            if endpoint.max_packet_size == 0:
                issues.append("endpoint 0x%02x has zero wMaxPacketSize" % endpoint.address)
            if interface.alternate_setting == 0:
                active_endpoint_addresses.append(endpoint.address)
    if len(active_endpoint_addresses) != len(set(active_endpoint_addresses)):
        issues.append("active interfaces reuse an endpoint address")

    for association in configuration.associations:
        associated = set(
            range(
                association.first_interface,
                association.first_interface + association.interface_count,
            )
        )
        if association.interface_count == 0:
            issues.append("interface association has an empty interface range")
        elif not associated.issubset(interface_numbers):
            issues.append(
                "interface association starting at %d exceeds the configuration"
                % association.first_interface
            )

    for descriptor in configuration.unowned_descriptors:
        # OTG is a configuration-level descriptor and is expected to be unowned.
        if descriptor.descriptor_type == 0x09:
            if descriptor.length != 3:
                issues.append("OTG descriptor must be three bytes")
        else:
            issues.append(
                "descriptor type 0x%02x at offset %d has no owning interface"
                % (descriptor.descriptor_type, descriptor.offset)
            )
    return tuple(issues)


def validate_configuration_descriptor(
    data: Union[bytes, ConfigurationDescriptor]
) -> ConfigurationDescriptor:
    """Parse and validate a configuration, raising on the first issue set."""

    configuration = (
        data if isinstance(data, ConfigurationDescriptor) else parse_configuration_descriptor(data)
    )
    issues = configuration_descriptor_issues(configuration)
    if issues:
        raise USBProtocolError("invalid USB configuration: " + "; ".join(issues))
    return configuration


def encode_string_descriptor(value: str) -> bytes:
    """Encode one USB string descriptor as UTF-16LE."""

    if not isinstance(value, str):
        raise TypeError("USB strings must be text")
    encoded = value.encode("utf-16-le")
    length = len(encoded) + 2
    if length > 255:
        raise USBProtocolError("USB string descriptor exceeds 255 bytes")
    return bytes((length, USB_DT_STRING)) + encoded


def decode_string_descriptor(data: bytes) -> str:
    raw = bytes(data)
    if len(raw) < 2 or raw[0] != len(raw) or raw[1] != USB_DT_STRING:
        raise USBProtocolError("invalid USB string descriptor")
    if len(raw) % 2:
        raise USBProtocolError("USB string descriptor has an odd byte length")
    try:
        return raw[2:].decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise USBProtocolError("USB string descriptor is not UTF-16LE") from exc


class StringDescriptorTable:
    """English-US descriptor table, including Linux's allocated blank IDs."""

    def __init__(
        self,
        strings: Mapping[int, str],
        language_ids: Sequence[int] = (0x0409,),
    ) -> None:
        self.strings = {int(index): value for index, value in strings.items()}
        self.language_ids = tuple(int(language) for language in language_ids)
        if not self.language_ids:
            raise ValueError("at least one USB language ID is required")
        for index in self.strings:
            if not 1 <= index <= 0xFF:
                raise ValueError("USB string indices must be in [1, 255]")

    def descriptor(self, index: int, language_id: int = 0x0409) -> bytes:
        if index == 0:
            encoded = b"".join(struct.pack("<H", item) for item in self.language_ids)
            return bytes((len(encoded) + 2, USB_DT_STRING)) + encoded
        if language_id not in self.language_ids:
            raise USBProtocolError("unsupported USB language ID 0x%04x" % language_id)
        try:
            value = self.strings[index]
        except KeyError as exc:
            raise USBProtocolError("unknown USB string index %d" % index) from exc
        return encode_string_descriptor(value)


class USBPersonality(str, Enum):
    NORMAL = "normal"
    DFU = "dfu"


@dataclass(frozen=True)
class USBPersonalityMetadata:
    name: str
    vendor_id: int
    product_id: int
    device_version: int
    usb_version: int
    manufacturer: str
    product: str
    interface_count: int
    endpoint_addresses: Tuple[int, ...]
    functions: Tuple[str, ...]
    alternate_settings: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ObservedUSBProfile:
    schema_version: int
    capture: Mapping[str, object]
    device_descriptor: bytes
    configuration_descriptor: bytes
    strings: Mapping[int, str]
    language_ids: Tuple[int, ...]
    normal: USBPersonalityMetadata
    dfu: USBPersonalityMetadata
    iio_interface: int
    iio_pipe_count: int

    @property
    def parsed_device(self) -> DeviceDescriptor:
        return parse_device_descriptor(self.device_descriptor)

    @property
    def parsed_configuration(self) -> ConfigurationDescriptor:
        return validate_configuration_descriptor(self.configuration_descriptor)

    def string_table(self, serial: Optional[str] = None) -> StringDescriptorTable:
        values = dict(self.strings)
        if serial is not None:
            values[self.parsed_device.serial_number_index] = serial
        return StringDescriptorTable(values, self.language_ids)

    def personality(self, name: Union[str, USBPersonality]) -> USBPersonalityMetadata:
        personality = USBPersonality(name)
        return self.normal if personality is USBPersonality.NORMAL else self.dfu


def _integer_from_json(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise ValueError("expected an integer or base-prefixed integer string")


def _metadata_from_json(name: str, raw: Mapping[str, object]) -> USBPersonalityMetadata:
    return USBPersonalityMetadata(
        name=name,
        vendor_id=_integer_from_json(raw["vendor_id"]),
        product_id=_integer_from_json(raw["product_id"]),
        device_version=_integer_from_json(raw["device_version"]),
        usb_version=_integer_from_json(raw["usb_version"]),
        manufacturer=str(raw["manufacturer"]),
        product=str(raw["product"]),
        interface_count=int(raw["interface_count"]),
        endpoint_addresses=tuple(
            _integer_from_json(item) for item in raw.get("endpoint_addresses", [])
        ),
        functions=tuple(str(item) for item in raw.get("functions", [])),
        alternate_settings=tuple(
            str(item) for item in raw.get("alternate_settings", [])
        ),
    )


def load_observed_usb_profile(path: Optional[Union[str, Path]] = None) -> ObservedUSBProfile:
    """Load and cross-check the checked-in P210/Pluto USB observation."""

    source = (
        Path(path)
        if path is not None
        else Path(__file__).with_name("data") / "usb-p210-observed.json"
    )
    try:
        with source.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        normal_raw = raw["normal"]
        personalities = raw["personalities"]
        profile = ObservedUSBProfile(
            schema_version=int(raw["schema_version"]),
            capture=dict(raw.get("capture", {})),
            device_descriptor=bytes.fromhex(normal_raw["device_descriptor_hex"]),
            configuration_descriptor=bytes.fromhex(
                normal_raw["configuration_descriptor_hex"]
            ),
            strings={int(key): str(value) for key, value in normal_raw["strings"].items()},
            language_ids=tuple(
                _integer_from_json(value) for value in normal_raw["language_ids"]
            ),
            normal=_metadata_from_json("normal", personalities["normal"]),
            dfu=_metadata_from_json("dfu", personalities["dfu"]),
            iio_interface=int(normal_raw["native_iio"]["interface"]),
            iio_pipe_count=int(normal_raw["native_iio"]["pipe_count"]),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise USBProtocolError("cannot load observed USB profile %s: %s" % (source, exc)) from exc

    device = profile.parsed_device
    configuration = profile.parsed_configuration
    if (
        device.vendor_id != profile.normal.vendor_id
        or device.product_id != profile.normal.product_id
    ):
        raise USBProtocolError("normal personality VID:PID disagrees with device descriptor")
    if device.device_version != profile.normal.device_version:
        raise USBProtocolError("normal personality bcdDevice disagrees with device descriptor")
    if configuration.declared_interface_count != profile.normal.interface_count:
        raise USBProtocolError("normal personality interface count disagrees with descriptor")
    if configuration.endpoint_addresses != profile.normal.endpoint_addresses:
        raise USBProtocolError("normal personality endpoint map disagrees with descriptor")
    if profile.iio_interface not in {
        interface.number for interface in configuration.interfaces
    }:
        raise USBProtocolError("native-IIO interface is absent from the configuration")
    if profile.iio_pipe_count <= 0:
        raise USBProtocolError("native-IIO pipe count must be positive")
    return profile


@dataclass(frozen=True)
class DeterministicMACs:
    host: str
    device: str

    @property
    def host_address(self) -> str:
        return self.host

    @property
    def device_address(self) -> str:
        return self.device


def derive_deterministic_macs(serial: str) -> DeterministicMACs:
    """Reproduce ``S23udc``'s SHA-1 based host/device MAC derivation.

    BusyBox ``echo`` hashes the serial followed by one newline.  The first
    three host/device octets are ADI's firmware prefixes and the following
    three octets come from consecutive SHA-1 hex digits.
    """

    if not isinstance(serial, str) or not serial.rstrip("\r\n"):
        raise ValueError("serial must be non-empty text")
    normalized = serial.rstrip("\r\n")
    digest = hashlib.sha1((normalized + "\n").encode("utf-8")).hexdigest()

    def address(prefix: str, digits: str) -> str:
        suffix = ":".join(digits[index : index + 2] for index in range(0, 6, 2))
        return (prefix + ":" + suffix).lower()

    return DeterministicMACs(
        host=address("00:e0:22", digest[:6]),
        device=address("00:05:f7", digest[6:12]),
    )


# A descriptive alias useful to callers that do not know the firmware name.
derive_mac_addresses = derive_deterministic_macs


class USBControlEndpoint:
    """Deterministic EP0 state machine for the normal USB personality."""

    def __init__(
        self,
        profile: Optional[ObservedUSBProfile] = None,
        serial: Optional[str] = None,
    ) -> None:
        self.profile = profile or load_observed_usb_profile()
        self.device_descriptor = self.profile.device_descriptor
        self.configuration_descriptor = self.profile.configuration_descriptor
        self.strings = self.profile.string_table(serial)
        self.serial = self.strings.strings[
            self.profile.parsed_device.serial_number_index
        ]
        self.mac_addresses = derive_deterministic_macs(self.serial)
        self.configuration = 0
        self.open_pipes: Set[int] = set()

    @property
    def configured(self) -> bool:
        return self.configuration != 0

    @property
    def iio_pipe_states(self) -> Tuple[bool, ...]:
        return tuple(
            pipe in self.open_pipes for pipe in range(self.profile.iio_pipe_count)
        )

    def _coerce_setup(self, setup: Union[SetupPacket, bytes]) -> SetupPacket:
        if isinstance(setup, SetupPacket):
            return setup
        return SetupPacket.from_bytes(bytes(setup))

    def control_transfer(
        self, setup: Union[SetupPacket, bytes], payload: bytes = b""
    ) -> bytes:
        packet = self._coerce_setup(setup)
        data = bytes(payload)
        if packet.direction_in and data:
            raise USBProtocolError("control-read requests cannot include an OUT payload")

        if packet.request_type == (USB_DIR_IN | USB_TYPE_STANDARD | USB_RECIP_DEVICE):
            if packet.request == USB_REQ_GET_DESCRIPTOR:
                if data:
                    raise USBProtocolError("GET_DESCRIPTOR has no OUT data stage")
                return self._get_descriptor(packet)[: packet.length]
            if packet.request == USB_REQ_GET_CONFIGURATION:
                if packet.value or packet.index or packet.length != 1:
                    raise USBProtocolError("malformed GET_CONFIGURATION request")
                return bytes((self.configuration,))

        if packet.request_type == (USB_DIR_OUT | USB_TYPE_STANDARD | USB_RECIP_DEVICE):
            if packet.request == USB_REQ_SET_CONFIGURATION:
                if packet.index or packet.length or data or packet.value not in (0, 1):
                    raise USBProtocolError("malformed SET_CONFIGURATION request")
                self.configuration = packet.value
                self.open_pipes.clear()
                return b""

        if packet.request_type == (USB_DIR_OUT | USB_TYPE_VENDOR | USB_RECIP_INTERFACE):
            return self._native_iio_request(packet, data)

        raise USBProtocolError(
            "unsupported EP0 request type=0x%02x request=0x%02x"
            % (packet.request_type, packet.request)
        )

    # Common host-emulator spelling.
    handle_setup = control_transfer

    def reset(self) -> None:
        """Return EP0 and FunctionFS pipes to USB address-state semantics."""

        self.configuration = 0
        self.open_pipes.clear()

    def snapshot(self) -> Mapping[str, object]:
        """Return stable state suitable for the top-level twin snapshot."""

        device = self.profile.parsed_device
        return {
            "personality": USBPersonality.NORMAL.value,
            "vendor_id": device.vendor_id,
            "product_id": device.product_id,
            "bcd_device": device.device_version,
            "serial": self.serial,
            "configuration": self.configuration,
            "configured": self.configured,
            "iio_interface": self.profile.iio_interface,
            "iio_open_pipes": sorted(self.open_pipes),
            "host_mac": self.mac_addresses.host,
            "device_mac": self.mac_addresses.device,
        }

    def _get_descriptor(self, packet: SetupPacket) -> bytes:
        descriptor_type = packet.descriptor_type
        descriptor_index = packet.descriptor_index
        if descriptor_type == USB_DT_DEVICE:
            if descriptor_index or packet.index:
                raise USBProtocolError("device descriptor index and wIndex must be zero")
            return self.device_descriptor
        if descriptor_type == USB_DT_CONFIG:
            if descriptor_index or packet.index:
                raise USBProtocolError("configuration descriptor index and wIndex must be zero")
            return self.configuration_descriptor
        if descriptor_type == USB_DT_STRING:
            language = packet.index or self.strings.language_ids[0]
            return self.strings.descriptor(descriptor_index, language)
        raise USBProtocolError("unsupported descriptor type 0x%02x" % descriptor_type)

    def _native_iio_request(self, packet: SetupPacket, payload: bytes) -> bytes:
        if not self.configured:
            raise USBProtocolError("native-IIO request received while USB is unconfigured")
        if packet.index != self.profile.iio_interface or packet.length or payload:
            raise USBProtocolError("malformed native-IIO interface request")
        if packet.request == IIO_REQ_RESET_PIPES:
            if packet.value:
                raise USBProtocolError("RESET_PIPES requires wValue zero")
            self.open_pipes.clear()
            return b""
        if packet.request not in (IIO_REQ_OPEN_PIPE, IIO_REQ_CLOSE_PIPE):
            raise USBProtocolError("unknown native-IIO vendor request %d" % packet.request)
        pipe = packet.value
        if not 0 <= pipe < self.profile.iio_pipe_count:
            raise USBProtocolError("native-IIO pipe %d is out of range" % pipe)
        if packet.request == IIO_REQ_OPEN_PIPE:
            self.open_pipes.add(pipe)
        else:
            self.open_pipes.discard(pipe)
        return b""


# Names used by higher-level board models and tests.
P210USBDevice = USBControlEndpoint
USBDeviceTwin = USBControlEndpoint


__all__ = [
    "ConfigurationDescriptor",
    "DFU_PRODUCT_ID",
    "Descriptor",
    "DeterministicMACs",
    "DeviceDescriptor",
    "EndpointDescriptor",
    "IIO_INTERFACE_NUMBER",
    "IIO_PIPE_COUNT",
    "IIO_REQ_CLOSE_PIPE",
    "IIO_REQ_OPEN_PIPE",
    "IIO_REQ_RESET_PIPES",
    "InterfaceAssociationDescriptor",
    "InterfaceDescriptor",
    "NORMAL_PRODUCT_ID",
    "NORMAL_VENDOR_ID",
    "ObservedUSBProfile",
    "P210USBDevice",
    "SetupPacket",
    "StringDescriptorTable",
    "USBControlEndpoint",
    "USBDeviceTwin",
    "USBPersonality",
    "USBPersonalityMetadata",
    "configuration_descriptor_issues",
    "decode_string_descriptor",
    "derive_deterministic_macs",
    "derive_mac_addresses",
    "encode_string_descriptor",
    "load_observed_usb_profile",
    "parse_configuration_descriptor",
    "parse_descriptor_stream",
    "parse_device_descriptor",
    "validate_configuration_descriptor",
]
