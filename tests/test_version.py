"""Tests for version checking."""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path

import pytest

from prism_sync import (
    VersionError,
    VersionProfile,
    check_version,
    load_profiles,
    parse_profiles,
    read_local_version,
    resolve_remote_version,
    save_profiles,
    write_local_version,
)
from prism_sync.varint import encode_varint


def _create_version_string(
    simple: str = "2.19.1",
    checksum: int = 0x123456789ABCDEF0,
    size: int = 1024,
    seed: int = 0xFEDCBA9876543210,
) -> str:
    """Create a valid version string."""
    payload = (
        checksum.to_bytes(8, "big") + encode_varint(size) + seed.to_bytes(8, "big")
    )
    encoded = base64.b64encode(payload).decode("ascii").rstrip("=")
    return f"{simple}@{encoded}"


def _test_profile(game_version: str = "9.9.9") -> VersionProfile:
    """Build a synthetic profile; the library no longer ships a default."""
    return VersionProfile(game_version=game_version, version_hash="test-hash")


class TestVersionProfile:
    """Tests for VersionProfile dataclass."""

    def test_creation(self) -> None:
        """Test VersionProfile creation."""
        profile = VersionProfile(
            game_version="2.19.1",
            version_hash="abc123",
        )
        assert profile.game_version == "2.19.1"
        assert profile.version_hash == "abc123"
        assert profile.target == "default.api.imassc.song4.prism.bn765.com:443"
        assert profile.resource_target == "production"
        assert profile.application_platform == "google"
        assert profile.os_name == "android"

    def test_metadata(self) -> None:
        """Test metadata method."""
        profile = VersionProfile(
            game_version="2.19.1",
            version_hash="abc123",
        )
        metadata = profile.metadata("2.19.1@test")
        assert len(metadata) == 6
        assert ("x-application-platform", "google") in metadata
        assert ("x-os-name", "android") in metadata
        assert ("x-application-version", "2.19.1") in metadata
        assert ("x-version-hash", "abc123") in metadata
        assert ("x-resource-target", "production") in metadata
        assert ("x-resource-version", "2.19.1@test") in metadata

    def test_frozen(self) -> None:
        """Test that VersionProfile is frozen."""
        profile = VersionProfile(
            game_version="2.19.1",
            version_hash="abc123",
        )
        with pytest.raises(AttributeError):
            profile.game_version = "2.20.0"  # type: ignore


class TestNoBuiltInProfile:
    """The library must not ship a game version or version hash."""

    def test_default_profile_is_not_exported(self) -> None:
        """``default_profile`` is gone; callers build a profile themselves."""
        import prism_sync

        assert not hasattr(prism_sync, "default_profile")
        assert "default_profile" not in prism_sync.__all__

    def test_profile_must_be_built_explicitly(self) -> None:
        """A profile carries exactly what the caller supplied."""
        profile = _test_profile("42.0.1")
        assert profile.game_version == "42.0.1"
        assert profile.version_hash == "test-hash"


class TestReadLocalVersion:
    """Tests for read_local_version function."""

    def test_nonexistent_file(self) -> None:
        """Test reading from non-existent file returns None."""
        result = read_local_version("/tmp/nonexistent")
        assert result is None

    def test_valid_file(self) -> None:
        """Test reading from valid file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            version_str = _create_version_string()
            write_local_version(
                tmpdir,
                game_version="2.19.1",
                resource_version=version_str,
                catalog_fingerprint="sha256...",
            )
            result = read_local_version(tmpdir)
            assert result is not None
            assert result.game_version == "2.19.1"
            assert result.resource_version == version_str
            assert result.catalog_fingerprint == "sha256..."
            assert result.synced_at is not None

    def test_corrupt_file(self) -> None:
        """Test reading from corrupt file returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / ".prism-version.json"
            path.write_text("invalid json", encoding="utf-8")
            result = read_local_version(tmpdir)
            assert result is None

    def test_missing_fields(self) -> None:
        """Test reading from file with missing fields returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / ".prism-version.json"
            path.write_text('{"gameVersion": "2.19.1"}', encoding="utf-8")
            result = read_local_version(tmpdir)
            assert result is None


class TestWriteLocalVersion:
    """Tests for write_local_version function."""

    def test_write_version(self) -> None:
        """Test writing version marker."""
        with tempfile.TemporaryDirectory() as tmpdir:
            version_str = _create_version_string()
            write_local_version(
                tmpdir,
                game_version="2.19.1",
                resource_version=version_str,
                catalog_fingerprint="sha256...",
                selection={"types": ["json"]},
            )
            path = Path(tmpdir) / ".prism-version.json"
            assert path.is_file()
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data["gameVersion"] == "2.19.1"
            assert data["resourceVersion"] == version_str
            assert data["catalogFingerprint"] == "sha256..."
            assert data["selection"] == {"types": ["json"]}
            assert "syncedAt" in data


class TestCheckVersion:
    """Tests for check_version function."""

    def test_no_local_version(self) -> None:
        """Test with no local version."""
        with tempfile.TemporaryDirectory() as tmpdir:
            profile = _test_profile()
            version_str = _create_version_string()
            check = check_version(profile, tmpdir, version_str)
            assert check.local is None
            assert check.up_to_date is False
            assert check.reason == "no local version marker"

    def test_up_to_date(self) -> None:
        """Test when versions match."""
        with tempfile.TemporaryDirectory() as tmpdir:
            profile = _test_profile()
            version_str = _create_version_string()
            write_local_version(
                tmpdir,
                game_version=profile.game_version,
                resource_version=version_str,
                catalog_fingerprint="sha256...",
            )
            check = check_version(profile, tmpdir, version_str)
            assert check.local is not None
            assert check.up_to_date is True
            assert check.reason == "up to date"

    def test_resource_version_changed(self) -> None:
        """Test when resource version changed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            profile = _test_profile()
            version_str1 = _create_version_string(simple=profile.game_version)
            version_str2 = _create_version_string(simple="9.9.10")
            write_local_version(
                tmpdir,
                game_version=profile.game_version,
                resource_version=version_str1,
                catalog_fingerprint="sha256...",
            )
            check = check_version(profile, tmpdir, version_str2)
            assert check.up_to_date is False
            assert "resource version changed" in check.reason

    def test_game_version_changed(self) -> None:
        """Test when game version changed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            profile = VersionProfile(
                game_version="2.20.0",
                version_hash="abc123",
            )
            version_str = _create_version_string()
            write_local_version(
                tmpdir,
                game_version="2.19.1",
                resource_version=version_str,
                catalog_fingerprint="sha256...",
            )
            check = check_version(profile, tmpdir, version_str)
            assert check.up_to_date is False
            assert "game version changed" in check.reason

    def test_invalid_remote_version(self) -> None:
        """Test with invalid remote version."""
        with tempfile.TemporaryDirectory() as tmpdir:
            profile = _test_profile()
            with pytest.raises(
                VersionError, match="remote resource version is invalid"
            ):
                check_version(profile, tmpdir, "invalid")


class TestResolveRemoteVersion:
    """Tests for resolve_remote_version function."""

    def test_explicit_version(self) -> None:
        """Test with explicit version."""
        profile = _test_profile()
        version_str = _create_version_string()
        result = resolve_remote_version(profile, resource_version=version_str)
        assert result == version_str

    def test_custom_fetcher(self) -> None:
        """Test with custom fetcher."""
        profile = _test_profile()
        version_str = _create_version_string()
        result = resolve_remote_version(
            profile,
            fetcher=lambda p: version_str,
        )
        assert result == version_str

    def test_invalid_explicit_version(self) -> None:
        """Test with invalid explicit version."""
        profile = _test_profile()
        with pytest.raises(ValueError, match="resource version must contain"):
            resolve_remote_version(profile, resource_version="invalid")


class TestParseProfiles:
    """Tests for parse_profiles function."""

    def test_single_profile(self) -> None:
        """Test parsing single profile object."""
        raw = {
            "gameVersion": "2.19.1",
            "versionHash": "abc123",
        }
        profiles, default_name = parse_profiles(raw)
        assert default_name == "default"
        assert "default" in profiles
        assert profiles["default"].game_version == "2.19.1"
        assert profiles["default"].version_hash == "abc123"

    def test_profiles_dict(self) -> None:
        """Test parsing profiles dict."""
        raw = {
            "default": "2.19.1",
            "profiles": {
                "2.19.1": {"gameVersion": "2.19.1", "versionHash": "abc123"},
                "2.20.0": {"gameVersion": "2.20.0", "versionHash": "def456"},
            },
        }
        profiles, default_name = parse_profiles(raw)
        assert default_name == "2.19.1"
        assert "2.19.1" in profiles
        assert "2.20.0" in profiles

    def test_invalid_data(self) -> None:
        """Test parsing invalid data raises error."""
        with pytest.raises(VersionError, match="profile data is not a JSON object"):
            parse_profiles(["not", "a", "dict"])

    def test_empty_profiles(self) -> None:
        """Test parsing empty profiles raises error."""
        with pytest.raises(VersionError, match="profile data has no profiles"):
            parse_profiles({})


class TestLoadProfiles:
    """Tests for load_profiles function."""

    def test_none_path_raises(self) -> None:
        """``None`` is rejected: the library ships no built-in profile."""
        with pytest.raises(TypeError, match="explicit profile file path"):
            load_profiles(None)  # type: ignore[arg-type]

    def test_nonexistent_file_raises(self, tmp_path: Path) -> None:
        """A missing file is an error, not a silent fallback."""
        missing = tmp_path / "nonexistent.json"
        with pytest.raises(VersionError, match="profile file not found"):
            load_profiles(missing)

    def test_valid_file(self) -> None:
        """Test loading from valid file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "profiles.json"
            path.write_text(
                json.dumps(
                    {
                        "default": "2.19.1",
                        "profiles": {
                            "2.19.1": {
                                "gameVersion": "2.19.1",
                                "versionHash": "abc123",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            profiles, _default_name = load_profiles(path)
            assert "2.19.1" in profiles


class TestSaveProfiles:
    """Tests for save_profiles function."""

    def test_save_profiles(self) -> None:
        """Test saving profiles."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "profiles.json"
            profiles = {
                "2.19.1": VersionProfile("2.19.1", "abc123"),
                "2.20.0": VersionProfile("2.20.0", "def456"),
            }
            save_profiles(path, profiles, "2.19.1")
            assert path.is_file()
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data["default"] == "2.19.1"
            assert "profiles" in data

    def test_empty_profiles_raises(self) -> None:
        """Test saving empty profiles raises error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "profiles.json"
            with pytest.raises(VersionError, match="cannot save an empty profile set"):
                save_profiles(path, {}, "default")


class TestVersionError:
    """Tests for VersionError exception."""

    def test_message(self) -> None:
        """Test exception message."""
        exc = VersionError("test error")
        assert str(exc) == "test error"
        assert isinstance(exc, RuntimeError)
