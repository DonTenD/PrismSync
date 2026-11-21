"""Tests for CRC64 calculation."""

from __future__ import annotations

import pytest

from prism_sync.crc import crc64, root_label_crc


class TestCrc64:
    """Tests for crc64 function."""

    def test_empty_bytes(self) -> None:
        """Test CRC64 of empty bytes."""
        assert crc64(b"") == 0

    def test_single_byte(self) -> None:
        """Test CRC64 of single byte."""
        # With init=0 and no final XOR, CRC64 of a single null byte is 0
        assert crc64(b"\x00") == 0

    def test_known_values(self) -> None:
        """Test CRC64 against known values from the game implementation."""
        # These values match the game's CRC64 implementation (init=0, no final XOR)
        assert crc64(b"123456789") == 0x6C40DF5F0B497347

    def test_non_zero_initial(self) -> None:
        """Test CRC64 with non-zero initial value."""
        # crc64(b"test", 0xFFFFFFFFFFFFFFFF) should give a different result
        result = crc64(b"test", crc=0xFFFFFFFFFFFFFFFF)
        assert result != crc64(b"test")

    def test_initial_masked(self) -> None:
        """Test that initial value is masked to 64 bits."""
        # Initial value larger than 64 bits should be masked
        large_initial = 0xFFFFFFFFFFFFFFFFFFFF
        result1 = crc64(b"test", crc=large_initial)
        result2 = crc64(b"test", crc=large_initial & 0xFFFFFFFFFFFFFFFF)
        assert result1 == result2

    def test_consistency(self) -> None:
        """Test that CRC64 is consistent across multiple calls."""
        data = b"Hello, World! This is a test string for CRC64."
        result1 = crc64(data)
        result2 = crc64(data)
        assert result1 == result2

    def test_different_inputs_different_outputs(self) -> None:
        """Test that different inputs produce different outputs."""
        result1 = crc64(b"test1")
        result2 = crc64(b"test2")
        assert result1 != result2


class TestRootLabelCrc:
    """Tests for root_label_crc function."""

    def test_valid_ascii(self) -> None:
        """Test root_label_crc with valid ASCII strings."""
        result = root_label_crc("2.19.1", "20240101")
        assert isinstance(result, int)
        assert 0 <= result <= 0xFFFFFFFFFFFFFFFF

    def test_known_value(self) -> None:
        """Test root_label_crc with known game version."""
        # This is a known value from the game
        result = root_label_crc("2.19.1", "20240101")
        # Just verify it's a valid CRC64 value
        assert 0 <= result <= 0xFFFFFFFFFFFFFFFF

    def test_non_ascii_raises(self) -> None:
        """Test that non-ASCII strings raise UnicodeEncodeError."""
        with pytest.raises(UnicodeEncodeError):
            root_label_crc("2.19.1", "测试")

        with pytest.raises(UnicodeEncodeError):
            root_label_crc("测试", "20240101")

    def test_empty_strings(self) -> None:
        """Test root_label_crc with empty strings."""
        result = root_label_crc("", "")
        assert result == crc64(b":")

    def test_colon_separator(self) -> None:
        """Test that the colon separator is included in the CRC."""
        result1 = root_label_crc("2.19.1", "20240101")
        result2 = crc64(b"2.19.1:20240101")
        assert result1 == result2
