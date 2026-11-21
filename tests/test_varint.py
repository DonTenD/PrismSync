"""Tests for varint encoding and decoding."""

from __future__ import annotations

import pytest

from prism_sync.varint import decode_varint, encode_varint


class TestEncodeVarint:
    """Tests for encode_varint function."""

    def test_zero(self) -> None:
        """Test encoding zero."""
        assert encode_varint(0) == b"\x00"

    def test_small_values(self) -> None:
        """Test encoding small positive values."""
        assert encode_varint(1) == b"\x01"
        assert encode_varint(127) == b"\x7f"
        assert encode_varint(128) == b"\x80\x01"
        assert encode_varint(255) == b"\xff\x01"

    def test_medium_values(self) -> None:
        """Test encoding medium values."""
        assert encode_varint(16383) == b"\xff\x7f"
        assert encode_varint(16384) == b"\x80\x80\x01"

    def test_large_values(self) -> None:
        """Test encoding large values."""
        # 2^64 - 1
        max_64 = (1 << 64) - 1
        result = encode_varint(max_64)
        # Should be 10 bytes (7 bits * 9 = 63, need 10th byte for last bit)
        assert len(result) == 10
        # Verify it decodes back
        decoded, _ = decode_varint(result)
        assert decoded == max_64

    def test_negative_raises(self) -> None:
        """Test that negative values raise ValueError."""
        with pytest.raises(ValueError, match="varint cannot encode a negative value"):
            encode_varint(-1)

    def test_too_large_raises(self) -> None:
        """Test that values exceeding 64 bits raise ValueError."""
        with pytest.raises(ValueError, match="varint value exceeds 64 bits"):
            encode_varint(1 << 64)


class TestDecodeVarint:
    """Tests for decode_varint function."""

    def test_zero(self) -> None:
        """Test decoding zero."""
        value, pos = decode_varint(b"\x00")
        assert value == 0
        assert pos == 1

    def test_small_values(self) -> None:
        """Test decoding small positive values."""
        value, pos = decode_varint(b"\x01")
        assert value == 1
        assert pos == 1

        value, pos = decode_varint(b"\x7f")
        assert value == 127
        assert pos == 1

    def test_medium_values(self) -> None:
        """Test decoding medium values."""
        value, pos = decode_varint(b"\x80\x01")
        assert value == 128
        assert pos == 2

        value, pos = decode_varint(b"\xff\x01")
        assert value == 255
        assert pos == 2

    def test_with_position(self) -> None:
        """Test decoding with starting position."""
        data = b"\x00\x7f\x80\x01"
        value, pos = decode_varint(data, pos=1)
        assert value == 127
        assert pos == 2

        value, pos = decode_varint(data, pos=2)
        assert value == 128
        assert pos == 4

    def test_negative_position_raises(self) -> None:
        """Test that negative position raises ValueError."""
        with pytest.raises(ValueError, match="pos must be non-negative"):
            decode_varint(b"\x00", pos=-1)

    def test_truncated_raises(self) -> None:
        """Test that truncated data raises ValueError."""
        # Incomplete varint (continuation bit set but no more data)
        with pytest.raises(ValueError, match="truncated varint"):
            decode_varint(b"\x80")

    def test_too_long_raises(self) -> None:
        """Test that over-long encoding raises ValueError."""
        # 11 bytes with continuation bit set on the 11th byte
        data = b"\x80" * 11
        with pytest.raises(ValueError, match="varint is longer than 10 bytes"):
            decode_varint(data)

    def test_bytearray_input(self) -> None:
        """Test decoding from bytearray."""
        data = bytearray(b"\x80\x01")
        value, pos = decode_varint(data)
        assert value == 128
        assert pos == 2

    def test_memoryview_input(self) -> None:
        """Test decoding from memoryview."""
        data = memoryview(b"\x80\x01")
        value, pos = decode_varint(data)
        assert value == 128
        assert pos == 2


class TestVarintRoundtrip:
    """Tests for varint encoding and decoding roundtrip."""

    @pytest.mark.parametrize("value", [0, 1, 127, 128, 255, 16383, 16384, 1000000])
    def test_roundtrip_small_values(self, value: int) -> None:
        """Test roundtrip for various small values."""
        encoded = encode_varint(value)
        decoded, _ = decode_varint(encoded)
        assert decoded == value

    @pytest.mark.parametrize(
        "value",
        [
            (1 << 64) - 1,
            (1 << 63) - 1,
            (1 << 62) - 1,
            (1 << 56) - 1,
        ],
    )
    def test_roundtrip_large_values(self, value: int) -> None:
        """Test roundtrip for large values."""
        encoded = encode_varint(value)
        decoded, _ = decode_varint(encoded)
        assert decoded == value
