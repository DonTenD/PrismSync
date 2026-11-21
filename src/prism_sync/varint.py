"""LEB128 variable-length integers used by the catalog format."""

from __future__ import annotations

MASK64 = (1 << 64) - 1


def encode_varint(n: int) -> bytes:
    """Encode a non-negative integer as an unsigned LEB128 varint.

    The game's writer emits the minimal number of bytes and stops as soon as
    the remaining value is zero.
    """
    if n < 0:
        raise ValueError(f"varint cannot encode a negative value: {n}")
    if n > MASK64:
        raise ValueError(f"varint value exceeds 64 bits: {n}")

    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def decode_varint(raw: bytes | bytearray | memoryview, pos: int = 0) -> tuple[int, int]:
    """Decode an unsigned LEB128 varint starting at *pos*.

    Returns ``(value, new_pos)``.  The value is masked to 64 bits, matching the
    C# ``ulong`` arithmetic.  A truncated or over-long encoding raises
    :class:`ValueError` instead of silently wrapping.
    """
    if pos < 0:
        raise ValueError("pos must be non-negative")

    value = 0
    shift = 0
    length = len(raw)

    while True:
        if pos >= length:
            raise ValueError("truncated varint")
        byte = raw[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value & MASK64, pos
        shift += 7
        # The C# reader allows at most ten bytes (shift 0..63).  A continuation
        # bit after the tenth byte means the stream is malformed.
        if shift >= 70:
            raise ValueError("varint is longer than 10 bytes")
