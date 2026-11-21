"""Catalog binary parser.

The catalog is a big-endian structure-of-arrays.  Only the fields needed to
download assets are retained; dependency/group/content-address details are
consumed and discarded so the reader offset stays correct.
"""

from __future__ import annotations

import hashlib
import struct

from .compression import aes_ctr_decrypt, decompress_lz4_all
from .crc import ascii_bytes, crc64
from .manifest import CATALOG_MAGIC
from .models import CatalogEntry, ResourceType, UnsupportedResourceType
from .varint import decode_varint, encode_varint

CATALOG_MAGIC_NUMBER = 0xBC10
MAX_ENTRIES = 5_000_000
MAX_STRING_BYTES = 16 * 1024 * 1024


class CatalogFormatError(ValueError):
    """Raised when a catalog does not match the expected binary layout."""


class _Reader:
    """Bounds-checked big-endian reader."""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def _take(self, length: int) -> bytes:
        end = self.pos + length
        if end > len(self.data):
            raise CatalogFormatError(
                f"truncated catalog: need {length} bytes at offset {self.pos}, "
                f"only {len(self.data) - self.pos} remain"
            )
        value = self.data[self.pos : end]
        self.pos = end
        return value

    def u8(self) -> int:
        return self._take(1)[0]

    def u16(self) -> int:
        return int(struct.unpack(">H", self._take(2))[0])

    def u32(self) -> int:
        return int(struct.unpack(">I", self._take(4))[0])

    def u64(self) -> int:
        return int(struct.unpack(">Q", self._take(8))[0])

    def varint(self) -> int:
        try:
            value, self.pos = decode_varint(self.data, self.pos)
        except ValueError as exc:
            raise CatalogFormatError(str(exc)) from exc
        return value

    def cstr(self) -> str:
        end = self.data.find(b"\x00", self.pos)
        if end < 0:
            raise CatalogFormatError(f"unterminated string at offset {self.pos}")
        raw = self.data[self.pos : end]
        if len(raw) > MAX_STRING_BYTES:
            raise CatalogFormatError(
                f"string at offset {self.pos} is too long: {len(raw)} bytes"
            )
        self.pos = end + 1
        # Encoding.UTF8.GetString replaces invalid sequences with U+FFFD.
        return raw.decode("utf-8", errors="replace")

    def peek(self) -> int:
        if self.pos >= len(self.data):
            raise CatalogFormatError("unexpected end of catalog")
        return self.data[self.pos]

    def ensure_done(self) -> None:
        if self.pos != len(self.data):
            raise CatalogFormatError(
                f"catalog parser stopped at offset {self.pos}, "
                f"but {len(self.data) - self.pos} bytes remain"
            )


def _check_count(value: int, field: str) -> int:
    if value < 0:
        raise CatalogFormatError(f"{field} is negative: {value}")
    if value > MAX_ENTRIES:
        raise CatalogFormatError(f"{field} is too large: {value}")
    return value


def _as_i32(value: int) -> int:
    """Match C# ``(int)ReadVarUInt64()`` unchecked truncation."""
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def decode_catalog(seed: int, label_crc: int, size: int, blob: bytes) -> bytes:
    """Decrypt and LZ4-decompress a catalog object.

    The key material is::

        CATALOG_MAGIC (16 bytes)
        seed           (u64be)
        label_crc      (u64be)
        size           (varint)

    SHA-1 of that material gives ``key = digest[:16]`` and
    ``iv = digest[4:20]``.  The result must start with the ``0xBC10`` magic.
    """
    material = bytearray()
    material += CATALOG_MAGIC
    material += seed.to_bytes(8, "big")
    material += label_crc.to_bytes(8, "big")
    material += encode_varint(size)

    digest = hashlib.sha1(material).digest()
    plain = aes_ctr_decrypt(blob, digest[:16], digest[4:20])
    try:
        raw = decompress_lz4_all(plain)
    except ValueError as exc:
        raise CatalogFormatError(f"LZ4 decompression failed: {exc}") from exc

    if len(raw) < 2:
        raise CatalogFormatError("decoded catalog is shorter than 2 bytes")
    magic = int.from_bytes(raw[:2], "big")
    if magic != CATALOG_MAGIC_NUMBER:
        raise CatalogFormatError(
            f"invalid catalog magic: 0x{magic:04X}, "
            f"expected 0x{CATALOG_MAGIC_NUMBER:04X}"
        )
    return raw


def parse_catalog(data: bytes) -> list[CatalogEntry]:
    """Parse an already decrypted/decompressed catalog into entries.

    The parser intentionally does not build a full ``CatalogBinaryEntry``
    model.  Priorities, dependency CRCs, content-address CRCs and group
    relations are not needed for downloading; they are read and discarded.
    """
    reader = _Reader(data)

    magic = reader.u16()
    if magic != CATALOG_MAGIC_NUMBER:
        raise CatalogFormatError(
            f"invalid catalog magic: 0x{magic:04X}, "
            f"expected 0x{CATALOG_MAGIC_NUMBER:04X}"
        )

    entry_count = _check_count(reader.varint(), "entry count")
    type_count = _check_count(reader.varint(), "type count")
    group_count = _check_count(reader.varint(), "group count")

    types = [reader.cstr() for _ in range(type_count)]
    for _ in range(group_count):
        reader.cstr()

    priorities = [reader.varint() for _ in range(entry_count)]
    resource_types = [reader.varint() for _ in range(entry_count)]
    num_deps = [
        _check_count(reader.varint(), "dependency count") for _ in range(entry_count)
    ]
    num_contents = [
        _check_count(reader.varint(), "content count") for _ in range(entry_count)
    ]
    num_groups = [
        _check_count(reader.varint(), "group count") for _ in range(entry_count)
    ]
    sizes = [reader.varint() for _ in range(entry_count)]
    type_indexes = [
        _check_count(reader.varint(), "type index") for _ in range(entry_count)
    ]

    for index in range(entry_count):
        for _ in range(num_contents[index]):
            content_type = reader.varint()
            if content_type >= len(types):
                raise CatalogFormatError(
                    f"entry {index} references content type {content_type}, "
                    f"but only {len(types)} types exist"
                )

    for index in range(entry_count):
        for _ in range(num_groups[index]):
            group = reader.varint()
            if group >= group_count:
                raise CatalogFormatError(
                    f"entry {index} references group {group}, "
                    f"but only {group_count} groups exist"
                )

    labels: list[str] = []
    label_crcs: list[int] = []
    for index in range(entry_count):
        label = reader.cstr()
        type_index = type_indexes[index]
        if type_index >= len(types):
            raise CatalogFormatError(
                f"entry {index} has type index {type_index}, "
                f"but only {len(types)} types exist"
            )

        crc = crc64(ascii_bytes(label))
        crc = crc64(b".", crc)
        crc = crc64(ascii_bytes(types[type_index]), crc)
        labels.append(label)
        label_crcs.append(crc)

    # Content addresses and dependencies are not needed for downloading.
    for index in range(entry_count):
        for _ in range(num_contents[index]):
            reader.cstr()
    for index in range(entry_count):
        for _ in range(num_deps[index]):
            reader.cstr()

    checksums = [reader.u64() for _ in range(entry_count)]

    seeds: list[int] = []
    for _ in range(entry_count):
        # C#: (reader.Peek & 0x80) != 0 ? ReadUInt64() : ReadByte()
        if reader.peek() & 0x80:
            seeds.append(reader.u64())
        else:
            seeds.append(reader.u8())

    reader.ensure_done()

    entries: list[CatalogEntry] = []
    for index in range(entry_count):
        raw_type = resource_types[index]
        try:
            resource_type: ResourceType | None = ResourceType.from_value(raw_type)
        except UnsupportedResourceType:
            resource_type = None

        entries.append(
            CatalogEntry(
                label=labels[index],
                type_name=types[type_indexes[index]],
                size=sizes[index],
                checksum=checksums[index],
                seed=seeds[index],
                label_crc=label_crcs[index],
                priority=_as_i32(priorities[index]),
                resource_type=resource_type,
                resource_type_value=raw_type,
            )
        )

    return entries
