"""Tests for manifest parsing and name derivation."""

from __future__ import annotations

import base64
import hashlib

import pytest

from prism_sync.manifest import FullVersion, decode_full_version, real_name
from prism_sync.varint import encode_varint


class TestDecodeFullVersion:
    """Tests for decode_full_version function."""

    def test_valid_version(self) -> None:
        """Test decoding a valid resource version."""
        # Build a valid version string
        simple = "2.19.1"
        checksum = 0x123456789ABCDEF0
        size = 1024
        seed = 0xFEDCBA9876543210

        # Encode the payload
        payload = (
            checksum.to_bytes(8, "big") + encode_varint(size) + seed.to_bytes(8, "big")
        )
        encoded = base64.b64encode(payload).decode("ascii").rstrip("=")
        version_str = f"{simple}@{encoded}"

        result = decode_full_version(version_str)

        assert result.simple == simple
        assert result.checksum == checksum
        assert result.size == size
        assert result.seed == seed
        assert result.raw == payload

    def test_version_with_padding(self) -> None:
        """Test decoding a version with padding in base64."""
        simple = "2.19.1"
        checksum = 0x123456789ABCDEF0
        size = 1024
        seed = 0xFEDCBA9876543210

        payload = (
            checksum.to_bytes(8, "big") + encode_varint(size) + seed.to_bytes(8, "big")
        )
        # Use standard base64 with padding
        encoded = base64.b64encode(payload).decode("ascii")
        version_str = f"{simple}@{encoded}"

        result = decode_full_version(version_str)

        assert result.simple == simple
        assert result.checksum == checksum
        assert result.size == size
        assert result.seed == seed

    def test_missing_at_raises(self) -> None:
        """Test that missing @ raises ValueError."""
        with pytest.raises(ValueError, match="resource version must contain '@'"):
            decode_full_version("2.19.1")

    def test_empty_simple_raises(self) -> None:
        """Test that empty simple part raises ValueError."""
        with pytest.raises(
            ValueError, match="resource version has an empty simple part"
        ):
            decode_full_version("@base64")

    def test_empty_base64_raises(self) -> None:
        """Test that empty base64 part raises ValueError."""
        with pytest.raises(
            ValueError, match="resource version has an empty base64 part"
        ):
            decode_full_version("2.19.1@")

    def test_non_string_raises(self) -> None:
        """Test that non-string input raises TypeError."""
        with pytest.raises(TypeError, match="full_version must be a string"):
            decode_full_version(123)  # type: ignore

    def test_invalid_base64_raises(self) -> None:
        """Test that invalid base64 raises ValueError."""
        with pytest.raises(ValueError, match="invalid base64 in resource version"):
            decode_full_version("2.19.1@invalid!!!")

    def test_too_short_payload_raises(self) -> None:
        """Test that too short payload raises ValueError."""
        # Payload needs at least 17 bytes (8 + 1 + 8)
        too_short = base64.b64encode(b"\x00" * 16).decode("ascii")
        with pytest.raises(ValueError, match="resource version payload is too short"):
            decode_full_version(f"2.19.1@{too_short}")

    def test_unexpected_length_raises(self) -> None:
        """Test that unexpected payload length raises ValueError."""
        # Build a payload with extra bytes
        simple = "2.19.1"
        checksum = 0x123456789ABCDEF0
        size = 1024
        seed = 0xFEDCBA9876543210

        payload = (
            checksum.to_bytes(8, "big")
            + encode_varint(size)
            + seed.to_bytes(8, "big")
            + b"\x00\x00"  # Extra bytes
        )
        encoded = base64.b64encode(payload).decode("ascii").rstrip("=")
        version_str = f"{simple}@{encoded}"

        with pytest.raises(
            ValueError, match="resource version payload has an unexpected length"
        ):
            decode_full_version(version_str)


class TestRealName:
    """Tests for real_name function."""

    def test_valid_inputs(self) -> None:
        """Test real_name with valid inputs."""
        checksum = 0x123456789ABCDEF0
        size = 1024
        label_crc = 0xFEDCBA9876543210

        result = real_name(checksum, size, label_crc)

        # Verify it's a valid base32 string without padding
        assert result.isalnum() or result == ""
        assert "=" not in result
        assert len(result) > 0

    def test_checksum_bounds(self) -> None:
        """Test checksum bounds checking."""
        with pytest.raises(ValueError, match="checksum must fit in 64 bits"):
            real_name(0x10000000000000000, 1024, 0)

        with pytest.raises(ValueError, match="checksum must fit in 64 bits"):
            real_name(-1, 1024, 0)

    def test_label_crc_bounds(self) -> None:
        """Test label_crc bounds checking."""
        with pytest.raises(ValueError, match="label_crc must fit in 64 bits"):
            real_name(0, 1024, 0x10000000000000000)

        with pytest.raises(ValueError, match="label_crc must fit in 64 bits"):
            real_name(0, 1024, -1)

    def test_size_non_negative(self) -> None:
        """Test size must be non-negative."""
        with pytest.raises(ValueError, match="size must be non-negative"):
            real_name(0, -1, 0)

    def test_deterministic(self) -> None:
        """Test that real_name is deterministic."""
        checksum = 0x123456789ABCDEF0
        size = 1024
        label_crc = 0xFEDCBA9876543210

        result1 = real_name(checksum, size, label_crc)
        result2 = real_name(checksum, size, label_crc)

        assert result1 == result2

    def test_different_inputs_different_outputs(self) -> None:
        """Test that different inputs produce different outputs."""
        result1 = real_name(0x123456789ABCDEF0, 1024, 0xFEDCBA9876543210)
        result2 = real_name(0x123456789ABCDEF1, 1024, 0xFEDCBA9876543210)

        assert result1 != result2

    def test_algorithm_correctness(self) -> None:
        """Test that real_name uses the correct algorithm."""
        checksum = 0x123456789ABCDEF0
        size = 1024
        label_crc = 0xFEDCBA9876543210

        # Manually compute the expected result
        material = (
            checksum.to_bytes(8, "big")
            + encode_varint(size)
            + label_crc.to_bytes(8, "big")
        )
        digest = hashlib.md5(material, usedforsecurity=False).digest()
        expected = base64.b32encode(digest).decode("ascii").rstrip("=")

        result = real_name(checksum, size, label_crc)

        assert result == expected


class TestFullVersion:
    """Tests for FullVersion NamedTuple."""

    def test_namedtuple_behavior(self) -> None:
        """Test that FullVersion behaves as a NamedTuple."""
        version = FullVersion(
            simple="2.19.1",
            checksum=0x123456789ABCDEF0,
            size=1024,
            seed=0xFEDCBA9876543210,
            raw=b"\x00" * 17,
        )

        # Access by name
        assert version.simple == "2.19.1"
        assert version.checksum == 0x123456789ABCDEF0

        # Access by index
        assert version[0] == "2.19.1"
        assert version[1] == 0x123456789ABCDEF0

        # Unpacking
        simple, checksum, _size, _seed, _raw = version
        assert simple == "2.19.1"
        assert checksum == 0x123456789ABCDEF0

    def test_immutable(self) -> None:
        """Test that FullVersion is immutable."""
        version = FullVersion(
            simple="2.19.1",
            checksum=0x123456789ABCDEF0,
            size=1024,
            seed=0xFEDCBA9876543210,
            raw=b"\x00" * 17,
        )

        with pytest.raises(AttributeError):
            version.simple = "2.20.0"  # type: ignore

    def test_equality(self) -> None:
        """Test FullVersion equality."""
        version1 = FullVersion(
            simple="2.19.1",
            checksum=0x123456789ABCDEF0,
            size=1024,
            seed=0xFEDCBA9876543210,
            raw=b"\x00" * 17,
        )
        version2 = FullVersion(
            simple="2.19.1",
            checksum=0x123456789ABCDEF0,
            size=1024,
            seed=0xFEDCBA9876543210,
            raw=b"\x00" * 17,
        )

        assert version1 == version2

    def test_inequality(self) -> None:
        """Test FullVersion inequality."""
        version1 = FullVersion(
            simple="2.19.1",
            checksum=0x123456789ABCDEF0,
            size=1024,
            seed=0xFEDCBA9876543210,
            raw=b"\x00" * 17,
        )
        version2 = FullVersion(
            simple="2.20.0",
            checksum=0x123456789ABCDEF0,
            size=1024,
            seed=0xFEDCBA9876543210,
            raw=b"\x00" * 17,
        )

        assert version1 != version2
