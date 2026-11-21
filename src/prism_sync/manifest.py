"""Catalog manifest and CDN object-name derivation."""

from __future__ import annotations

import base64
import hashlib
from typing import NamedTuple

from .varint import decode_varint, encode_varint

#: 16-byte prefix used when deriving the root catalog AES key.
CATALOG_MAGIC = bytes.fromhex("E040926FB42EBD50886A8849E5545924")


class FullVersion(NamedTuple):
    """Decoded ``simple@base64`` resource version.

    ``raw`` is the decoded base64 payload, kept for diagnostics and tests.
    """

    simple: str
    checksum: int
    size: int
    seed: int
    raw: bytes


def decode_full_version(full_version: str) -> FullVersion:
    """Decode ``<simple>@<base64>`` into its catalog manifest fields.

    The base64 payload is laid out as::

        u64be checksum
        varint size
        u64be seed

    ``Convert.FromBase64String`` requires padding, but resource versions seen
    in the wild may omit it, so padding is restored before decoding.
    """
    if not isinstance(full_version, str):
        raise TypeError("full_version must be a string")
    if "@" not in full_version:
        raise ValueError("resource version must contain '@'")

    simple, encoded = full_version.split("@", 1)
    if not simple:
        raise ValueError("resource version has an empty simple part")
    if not encoded:
        raise ValueError("resource version has an empty base64 part")

    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        raw = base64.b64decode(padded, validate=True)
    except Exception as exc:  # binascii.Error is not stable across versions
        raise ValueError(f"invalid base64 in resource version: {exc}") from exc

    if len(raw) < 17:
        raise ValueError(f"resource version payload is too short: {len(raw)} bytes")

    checksum = int.from_bytes(raw[0:8], "big")
    size, pos = decode_varint(raw, 8)
    if pos + 8 != len(raw):
        raise ValueError(
            "resource version payload has an unexpected length: "
            f"{len(raw)} bytes, size varint ends at {pos}"
        )
    seed = int.from_bytes(raw[pos : pos + 8], "big")
    return FullVersion(simple=simple, checksum=checksum, size=size, seed=seed, raw=raw)


def real_name(checksum: int, size: int, label_crc: int) -> str:
    """Return the CDN object name for a catalog entry.

    ``base32(MD5(checksum_be64 || varint(size) || label_crc_be64))`` with the
    RFC4648 padding removed.
    """
    if not 0 <= checksum <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("checksum must fit in 64 bits")
    if not 0 <= label_crc <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("label_crc must fit in 64 bits")
    if size < 0:
        raise ValueError("size must be non-negative")

    material = (
        checksum.to_bytes(8, "big") + encode_varint(size) + label_crc.to_bytes(8, "big")
    )
    digest = hashlib.md5(material, usedforsecurity=False).digest()
    return base64.b32encode(digest).decode("ascii").rstrip("=")
