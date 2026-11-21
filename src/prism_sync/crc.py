"""CRC helpers matching the game's C# implementation.

Two details are easy to get wrong:

* ``crc64`` is CRC-64/ECMA-182 with ``init=0`` and **no** final XOR.  It is not
  the usual CRC-64/XZ variant.
* The C# ``Crc64(ReadOnlySpan<char>)`` overload feeds each ``char`` value
  directly into the lookup table.  For ASCII strings this is identical to
  running the byte overload over the ASCII bytes.  It is *not* UTF-16LE.
"""

from __future__ import annotations

MASK64 = (1 << 64) - 1
_CRC64_POLY = 0x42F0E1EBA9EA3693


def _build_crc64_table() -> tuple[int, ...]:
    table: list[int] = []
    for index in range(256):
        value = index << 56
        for _ in range(8):
            if value & (1 << 63):
                value = ((value << 1) ^ _CRC64_POLY) & MASK64
            else:
                value = (value << 1) & MASK64
        table.append(value)
    return tuple(table)


_CRC64_TABLE = _build_crc64_table()


def crc64(data: bytes, crc: int = 0) -> int:
    """Return CRC-64/ECMA-182 of *data*.

    Args:
        data: Bytes to hash.
        crc: Initial register value.  The game always starts at ``0``.

    Returns:
        The 64-bit CRC as an unsigned integer.
    """
    crc &= MASK64
    for byte in data:
        crc = _CRC64_TABLE[(byte ^ (crc >> 56)) & 0xFF] ^ ((crc << 8) & MASK64)
    return crc


def ascii_bytes(value: str) -> bytes:
    """Reproduce C# ``Encoding.ASCII.GetBytes``.

    Non-ASCII characters become ``?`` (0x3F), exactly like the .NET ASCII
    encoder.  The catalog parser uses this for labels, addresses, dependency
    names and group names.
    """
    return value.encode("ascii", "replace")


def root_label_crc(game_version: str, simple_version: str) -> int:
    """CRC64 of ``"<game_version>:<simple_version>"``.

    The C# code uses the ``ReadOnlySpan<char>`` overload.  For ASCII strings
    this is exactly the byte CRC; non-ASCII characters are rejected here
    because the C# lookup would index past its 256-entry table.
    """
    text = f"{game_version}:{simple_version}"
    return crc64(text.encode("ascii"))
