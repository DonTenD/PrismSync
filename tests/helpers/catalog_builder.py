"""Build synthetic catalogs in the real on-the-wire format.

The bytes produced here go through the production decoding path
(``decode_catalog`` -> ``parse_catalog``), so the integration tests exercise
real parsing instead of a stub.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field

import lz4.frame
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from prism_sync.catalog import CATALOG_MAGIC_NUMBER
from prism_sync.crc import ascii_bytes, crc64, root_label_crc
from prism_sync.manifest import CATALOG_MAGIC, real_name
from prism_sync.varint import encode_varint

#: Catalog ``type_index`` pointing at a sub-catalog entry.
CATALOG_TYPE_INDEX = 0


@dataclass(frozen=True)
class SpecEntry:
    """One entry in a synthetic catalog."""

    label: str
    type_name: str
    resource_type: int
    size: int
    checksum: int
    seed: int = 0

    def label_crc(self) -> int:
        """Return the CRC64 of ``"<label>.<type_name>"`` as the parser computes it."""
        crc = crc64(ascii_bytes(self.label))
        crc = crc64(b".", crc)
        return crc64(ascii_bytes(self.type_name), crc)

    def real_name(self) -> str:
        """Return the CDN object name for this entry."""
        return real_name(self.checksum, self.size, self.label_crc())


def _cstr(text: str) -> bytes:
    return text.encode("ascii") + b"\x00"


def encode_catalog(entries: list[SpecEntry], types: list[str]) -> bytes:
    """Serialise ``entries`` into the plaintext catalog format."""
    group_count = 0
    out = bytearray()
    out += CATALOG_MAGIC_NUMBER.to_bytes(2, "big")
    out += encode_varint(len(entries))
    out += encode_varint(len(types))
    out += encode_varint(group_count)

    for name in types:
        out += _cstr(name)

    for _entry in entries:
        out += encode_varint(0)  # priority
    for entry in entries:
        out += encode_varint(entry.resource_type)
    for _entry in entries:
        out += encode_varint(0)  # dependency count
    for _entry in entries:
        out += encode_varint(0)  # content count
    for _entry in entries:
        out += encode_varint(0)  # group count
    for entry in entries:
        out += encode_varint(entry.size)
    for entry in entries:
        out += encode_varint(types.index(entry.type_name))

    for entry in entries:
        out += _cstr(entry.label)

    for entry in entries:
        out += entry.checksum.to_bytes(8, "big")

    for entry in entries:
        seed = entry.seed
        if seed == 0 or seed < 0x80:
            out += seed.to_bytes(1, "big")
        else:
            out += b"\x80" + seed.to_bytes(8, "big")[1:]

    return bytes(out)


def encrypt_and_compress(
    plain: bytes, *, seed: int, label_crc: int, size: int
) -> bytes:
    """Apply the production catalog cipher and compression.

    Mirrors :func:`prism_sync.catalog.decode_catalog` exactly.
    """
    material = bytearray()
    material += CATALOG_MAGIC
    material += seed.to_bytes(8, "big")
    material += label_crc.to_bytes(8, "big")
    material += encode_varint(size)
    digest = hashlib.sha1(material).digest()

    compressed = lz4.frame.compress(plain)
    encryptor = Cipher(algorithms.AES(digest[:16]), modes.CTR(digest[4:20])).encryptor()
    return encryptor.update(compressed) + encryptor.finalize()


@dataclass
class SyntheticCatalog:
    """A whole synthetic catalog tree ready to serve over HTTP."""

    base_url: str = ""
    files: dict[str, bytes] = field(default_factory=dict)
    version: str = ""
    game_version: str = "9.9.9"
    simple: str = "12345678"

    def add_catalog(
        self,
        *,
        checksum: int,
        label_crc: int,
        seed: int,
        entries: list[SpecEntry],
        types: list[str],
    ) -> tuple[str, int]:
        """Encode one catalog object and register it for serving.

        ``decode_catalog`` derives its key from
        ``(seed, label_crc, manifest.size)`` and ``manifest.size`` comes from
        the version payload, so the catalogue is encrypted with ``storage_size``
        and both the object name and the version string must use that same
        value.  It is intentionally not the plaintext length.

        Args:
            checksum: Manifest checksum, also part of the object name.
            label_crc: Manifest label CRC.
            seed: Manifest seed.
            entries: Catalog entries.
            types: Type table the entries index into.

        Returns:
            The ``(real_name, storage_size)`` pair describing the object.
        """
        plain = encode_catalog(entries, types)
        storage_size = len(plain)
        blob = encrypt_and_compress(
            plain, seed=seed, label_crc=label_crc, size=storage_size
        )
        name = real_name(checksum, storage_size, label_crc)
        self.files[f"/r/{name[:2]}/{name}"] = blob
        return name, storage_size


def build_version(checksum: int, size: int, seed: int, simple: str) -> str:
    """Build the ``<simple>@<base64>`` resource version string."""
    payload = (
        checksum.to_bytes(8, "big") + encode_varint(size) + seed.to_bytes(8, "big")
    )
    return f"{simple}@{base64.b64encode(payload).decode('ascii')}"


def root_crc(game_version: str, simple: str) -> int:
    """Return the root catalog label CRC."""
    return root_label_crc(game_version, simple)
