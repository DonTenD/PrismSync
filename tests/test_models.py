"""Tests for domain models."""

from __future__ import annotations

from pathlib import Path

import pytest

from prism_sync.models import (
    AssetInfoEntry,
    CatalogAsset,
    CatalogEntry,
    CatalogManifest,
    LocalVersion,
    ResourceType,
    SyncOptions,
    SyncResult,
    UnsupportedResourceType,
    VersionCheck,
    VersionProfile,
)


class TestResourceType:
    """Tests for ResourceType enum."""

    def test_values(self) -> None:
        """Test ResourceType enum values."""
        assert ResourceType.BundlePlain.value == 0
        assert ResourceType.BundlePaddedStart.value == 1
        assert ResourceType.RawPlain.value == 64
        assert ResourceType.RawLz4Compressed.value == 65
        assert ResourceType.RawEncrypted.value == 66

    def test_from_value_valid(self) -> None:
        """Test from_value with valid values."""
        assert ResourceType.from_value(0) == ResourceType.BundlePlain
        assert ResourceType.from_value(1) == ResourceType.BundlePaddedStart
        assert ResourceType.from_value(64) == ResourceType.RawPlain
        assert ResourceType.from_value(65) == ResourceType.RawLz4Compressed
        assert ResourceType.from_value(66) == ResourceType.RawEncrypted

    def test_from_value_invalid(self) -> None:
        """Test from_value with invalid value."""
        with pytest.raises(UnsupportedResourceType) as exc_info:
            ResourceType.from_value(999)
        assert exc_info.value.value == 999

    def test_from_value_negative(self) -> None:
        """Test from_value with negative value."""
        with pytest.raises(UnsupportedResourceType) as exc_info:
            ResourceType.from_value(-1)
        assert exc_info.value.value == -1


class TestUnsupportedResourceType:
    """Tests for UnsupportedResourceType exception."""

    def test_message(self) -> None:
        """Test exception message."""
        exc = UnsupportedResourceType(999)
        assert "unsupported resource type: 999" in str(exc)
        assert exc.value == 999


class TestCatalogManifest:
    """Tests for CatalogManifest dataclass."""

    def test_creation(self) -> None:
        """Test CatalogManifest creation."""
        manifest = CatalogManifest(
            label_crc=0x123456789ABCDEF0,
            size=1024,
            checksum=0xFEDCBA9876543210,
            seed=0x0123456789ABCDEF,
        )

        assert manifest.label_crc == 0x123456789ABCDEF0
        assert manifest.size == 1024
        assert manifest.checksum == 0xFEDCBA9876543210
        assert manifest.seed == 0x0123456789ABCDEF

    def test_frozen(self) -> None:
        """Test that CatalogManifest is frozen."""
        manifest = CatalogManifest(
            label_crc=0x123456789ABCDEF0,
            size=1024,
            checksum=0xFEDCBA9876543210,
            seed=0x0123456789ABCDEF,
        )

        with pytest.raises(AttributeError):
            manifest.label_crc = 0  # type: ignore

    def test_real_name(self) -> None:
        """Test real_name property."""
        manifest = CatalogManifest(
            label_crc=0x123456789ABCDEF0,
            size=1024,
            checksum=0xFEDCBA9876543210,
            seed=0x0123456789ABCDEF,
        )

        # real_name should return a non-empty string
        assert isinstance(manifest.real_name(), str)
        assert len(manifest.real_name()) > 0

    def test_equality(self) -> None:
        """Test CatalogManifest equality."""
        manifest1 = CatalogManifest(
            label_crc=0x123456789ABCDEF0,
            size=1024,
            checksum=0xFEDCBA9876543210,
            seed=0x0123456789ABCDEF,
        )
        manifest2 = CatalogManifest(
            label_crc=0x123456789ABCDEF0,
            size=1024,
            checksum=0xFEDCBA9876543210,
            seed=0x0123456789ABCDEF,
        )

        assert manifest1 == manifest2

    def test_inequality(self) -> None:
        """Test CatalogManifest inequality."""
        manifest1 = CatalogManifest(
            label_crc=0x123456789ABCDEF0,
            size=1024,
            checksum=0xFEDCBA9876543210,
            seed=0x0123456789ABCDEF,
        )
        manifest2 = CatalogManifest(
            label_crc=0x123456789ABCDEF1,
            size=1024,
            checksum=0xFEDCBA9876543210,
            seed=0x0123456789ABCDEF,
        )

        assert manifest1 != manifest2


class TestCatalogEntry:
    """Tests for CatalogEntry dataclass."""

    def test_creation(self) -> None:
        """Test CatalogEntry creation."""
        entry = CatalogEntry(
            label="test_label",
            type_name="bytes",
            size=1024,
            checksum=0xFEDCBA9876543210,
            seed=0x0123456789ABCDEF,
            label_crc=0x123456789ABCDEF0,
            priority=1,
            resource_type=ResourceType.RawPlain,
            resource_type_value=64,
        )

        assert entry.label == "test_label"
        assert entry.type_name == "bytes"
        assert entry.size == 1024
        assert entry.checksum == 0xFEDCBA9876543210
        assert entry.seed == 0x0123456789ABCDEF
        assert entry.label_crc == 0x123456789ABCDEF0
        assert entry.priority == 1
        assert entry.resource_type == ResourceType.RawPlain
        assert entry.resource_type_value == 64

    def test_frozen(self) -> None:
        """Test that CatalogEntry is frozen."""
        entry = CatalogEntry(
            label="test_label",
            type_name="bytes",
            size=1024,
            checksum=0xFEDCBA9876543210,
            seed=0x0123456789ABCDEF,
            label_crc=0x123456789ABCDEF0,
            priority=1,
        )

        with pytest.raises(AttributeError):
            entry.label = "new_label"  # type: ignore

    def test_as_manifest(self) -> None:
        """Test as_manifest method."""
        entry = CatalogEntry(
            label="test_label",
            type_name="bytes",
            size=1024,
            checksum=0xFEDCBA9876543210,
            seed=0x0123456789ABCDEF,
            label_crc=0x123456789ABCDEF0,
            priority=1,
        )

        manifest = entry.as_manifest()

        assert isinstance(manifest, CatalogManifest)
        assert manifest.label_crc == entry.label_crc
        assert manifest.size == entry.size
        assert manifest.checksum == entry.checksum
        assert manifest.seed == entry.seed

    def test_resource_type_name_known(self) -> None:
        """Test resource_type_name with known type."""
        entry = CatalogEntry(
            label="test_label",
            type_name="bytes",
            size=1024,
            checksum=0xFEDCBA9876543210,
            seed=0x0123456789ABCDEF,
            label_crc=0x123456789ABCDEF0,
            priority=1,
            resource_type=ResourceType.RawPlain,
            resource_type_value=64,
        )

        assert entry.resource_type_name == "RawPlain"

    def test_resource_type_name_unknown(self) -> None:
        """Test resource_type_name with unknown type."""
        entry = CatalogEntry(
            label="test_label",
            type_name="bytes",
            size=1024,
            checksum=0xFEDCBA9876543210,
            seed=0x0123456789ABCDEF,
            label_crc=0x123456789ABCDEF0,
            priority=1,
            resource_type=None,
            resource_type_value=999,
        )

        assert entry.resource_type_name == "Unknown(999)"


class TestAssetInfoEntry:
    """Tests for AssetInfoEntry dataclass."""

    def test_creation(self) -> None:
        """Test AssetInfoEntry creation."""
        entry = AssetInfoEntry(
            name="test_name",
            path="root/raw/test",
            type_name="bytes",
        )

        assert entry.name == "test_name"
        assert entry.path == "root/raw/test"
        assert entry.type_name == "bytes"

    def test_to_dict(self) -> None:
        """Test to_dict method."""
        entry = AssetInfoEntry(
            name="test_name",
            path="root/raw/test",
            type_name="bytes",
        )

        result = entry.to_dict()

        assert result == {
            "Name": "test_name",
            "Path": "root/raw/test",
            "Type": "bytes",
        }

    def test_frozen(self) -> None:
        """Test that AssetInfoEntry is frozen."""
        entry = AssetInfoEntry(
            name="test_name",
            path="root/raw/test",
            type_name="bytes",
        )

        with pytest.raises(AttributeError):
            entry.name = "new_name"  # type: ignore


class TestCatalogAsset:
    """Tests for CatalogAsset dataclass."""

    def test_creation(self) -> None:
        """Test CatalogAsset creation."""
        entry = CatalogEntry(
            label="test_label",
            type_name="bytes",
            size=1024,
            checksum=0xFEDCBA9876543210,
            seed=0x0123456789ABCDEF,
            label_crc=0x123456789ABCDEF0,
            priority=1,
        )

        asset = CatalogAsset(
            entry=entry,
            catalog_label="root",
            output_path="root/raw/test.bytes",
            relative_path="raw/test.bytes",
            real_name="abc123",
        )

        assert asset.entry == entry
        assert asset.catalog_label == "root"
        assert asset.output_path == "root/raw/test.bytes"
        assert asset.relative_path == "raw/test.bytes"
        assert asset.real_name == "abc123"

    def test_frozen(self) -> None:
        """Test that CatalogAsset is frozen."""
        entry = CatalogEntry(
            label="test_label",
            type_name="bytes",
            size=1024,
            checksum=0xFEDCBA9876543210,
            seed=0x0123456789ABCDEF,
            label_crc=0x123456789ABCDEF0,
            priority=1,
        )

        asset = CatalogAsset(
            entry=entry,
            catalog_label="root",
            output_path="root/raw/test.bytes",
            relative_path="raw/test.bytes",
            real_name="abc123",
        )

        with pytest.raises(AttributeError):
            asset.catalog_label = "new_label"  # type: ignore


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

    def test_custom_values(self) -> None:
        """Test VersionProfile with custom values."""
        profile = VersionProfile(
            game_version="2.19.1",
            version_hash="abc123",
            target="custom.target:443",
            resource_target="staging",
            application_platform="ios",
            os_name="ios",
        )

        assert profile.target == "custom.target:443"
        assert profile.resource_target == "staging"
        assert profile.application_platform == "ios"
        assert profile.os_name == "ios"

    def test_metadata(self) -> None:
        """Test metadata method."""
        profile = VersionProfile(
            game_version="2.19.1",
            version_hash="abc123",
        )

        metadata = profile.metadata("2.19.1@base64")

        assert len(metadata) == 6
        assert ("x-application-platform", "google") in metadata
        assert ("x-os-name", "android") in metadata
        assert ("x-application-version", "2.19.1") in metadata
        assert ("x-version-hash", "abc123") in metadata
        assert ("x-resource-target", "production") in metadata
        assert ("x-resource-version", "2.19.1@base64") in metadata

    def test_frozen(self) -> None:
        """Test that VersionProfile is frozen."""
        profile = VersionProfile(
            game_version="2.19.1",
            version_hash="abc123",
        )

        with pytest.raises(AttributeError):
            profile.game_version = "2.20.0"  # type: ignore


class TestLocalVersion:
    """Tests for LocalVersion dataclass."""

    def test_creation(self) -> None:
        """Test LocalVersion creation."""
        local = LocalVersion(
            game_version="2.19.1",
            resource_version="2.19.1@base64",
        )

        assert local.game_version == "2.19.1"
        assert local.resource_version == "2.19.1@base64"
        assert local.catalog_fingerprint is None
        assert local.synced_at is None
        assert local.selection is None

    def test_with_optional_fields(self) -> None:
        """Test LocalVersion with optional fields."""
        local = LocalVersion(
            game_version="2.19.1",
            resource_version="2.19.1@base64",
            catalog_fingerprint="sha256...",
            synced_at="2024-01-01T00:00:00Z",
            selection={"types": ["json", "bytes"]},
        )

        assert local.catalog_fingerprint == "sha256..."
        assert local.synced_at == "2024-01-01T00:00:00Z"
        assert local.selection == {"types": ["json", "bytes"]}

    def test_frozen(self) -> None:
        """Test that LocalVersion is frozen."""
        local = LocalVersion(
            game_version="2.19.1",
            resource_version="2.19.1@base64",
        )

        with pytest.raises(AttributeError):
            local.game_version = "2.20.0"  # type: ignore


class TestVersionCheck:
    """Tests for VersionCheck dataclass."""

    def test_creation(self) -> None:
        """Test VersionCheck creation."""
        local = LocalVersion(
            game_version="2.19.1",
            resource_version="2.19.1@old",
        )

        check = VersionCheck(
            game_version="2.19.1",
            remote_resource_version="2.19.1@new",
            remote_simple="20240101",
            local=local,
            up_to_date=False,
            reason="remote version is newer",
        )

        assert check.game_version == "2.19.1"
        assert check.remote_resource_version == "2.19.1@new"
        assert check.remote_simple == "20240101"
        assert check.local == local
        assert check.up_to_date is False
        assert check.reason == "remote version is newer"

    def test_up_to_date(self) -> None:
        """Test VersionCheck with up_to_date=True."""
        local = LocalVersion(
            game_version="2.19.1",
            resource_version="2.19.1@current",
        )

        check = VersionCheck(
            game_version="2.19.1",
            remote_resource_version="2.19.1@current",
            remote_simple="20240101",
            local=local,
            up_to_date=True,
            reason="versions match",
        )

        assert check.up_to_date is True

    def test_no_local(self) -> None:
        """Test VersionCheck with no local version."""
        check = VersionCheck(
            game_version="2.19.1",
            remote_resource_version="2.19.1@new",
            remote_simple="20240101",
            local=None,
            up_to_date=False,
            reason="no local version",
        )

        assert check.local is None

    def test_frozen(self) -> None:
        """Test that VersionCheck is frozen."""
        check = VersionCheck(
            game_version="2.19.1",
            remote_resource_version="2.19.1@new",
            remote_simple="20240101",
            local=None,
            up_to_date=False,
            reason="no local version",
        )

        with pytest.raises(AttributeError):
            check.game_version = "2.20.0"  # type: ignore


class TestSyncOptions:
    """Tests for SyncOptions dataclass."""

    def test_creation(self) -> None:
        """Test SyncOptions creation."""
        options = SyncOptions(
            game_version="2.19.1",
            resource_version="2.19.1@base64",
            output=Path("/tmp/output"),
        )

        assert options.game_version == "2.19.1"
        assert options.resource_version == "2.19.1@base64"
        assert options.output == Path("/tmp/output")
        assert options.jobs == 8
        assert options.force is False
        assert options.catalog_only is False

    def test_default_values(self) -> None:
        """Test SyncOptions default values."""
        options = SyncOptions(
            game_version="2.19.1",
            resource_version="2.19.1@base64",
            output=Path("/tmp/output"),
        )

        assert options.jobs == 8
        assert options.force is False
        assert options.catalog_only is False
        assert options.prune is False
        assert options.no_localizetext is False
        assert options.verify is False
        assert options.deep is False
        assert options.resume is True
        assert options.dry_run is False
        assert options.stats_only is False
        assert options.catalog_cache is True
        assert options.refresh_catalogs is False
        assert options.timeout == 60.0
        assert options.retries == 3
        assert options.base_url == "https://asset.imassc.song4.prism.bn765.com"
        assert options.trust_env is True

    def test_custom_values(self) -> None:
        """Test SyncOptions with custom values."""
        options = SyncOptions(
            game_version="2.19.1",
            resource_version="2.19.1@base64",
            output=Path("/tmp/output"),
            jobs=4,
            force=True,
            timeout=120.0,
        )

        assert options.jobs == 4
        assert options.force is True
        assert options.timeout == 120.0

    def test_validate_valid(self) -> None:
        """Test validate with valid options."""
        options = SyncOptions(
            game_version="2.19.1",
            resource_version="2.19.1@base64",
            output=Path("/tmp/output"),
            jobs=4,
            timeout=30.0,
            retries=2,
        )

        # Should not raise
        options.validate()

    def test_validate_jobs_out_of_range(self) -> None:
        """Test validate with jobs out of range."""
        options = SyncOptions(
            game_version="2.19.1",
            resource_version="2.19.1@base64",
            output=Path("/tmp/output"),
            jobs=0,
        )

        with pytest.raises(ValueError, match="jobs must be between 1 and 16"):
            options.validate()

        options.jobs = 17
        with pytest.raises(ValueError, match="jobs must be between 1 and 16"):
            options.validate()

    def test_validate_negative_timeout(self) -> None:
        """Test validate with negative timeout."""
        options = SyncOptions(
            game_version="2.19.1",
            resource_version="2.19.1@base64",
            output=Path("/tmp/output"),
            timeout=-1.0,
        )

        with pytest.raises(ValueError, match="timeout must be positive"):
            options.validate()

    def test_validate_negative_retries(self) -> None:
        """Test validate with negative retries."""
        options = SyncOptions(
            game_version="2.19.1",
            resource_version="2.19.1@base64",
            output=Path("/tmp/output"),
            retries=-1,
        )

        with pytest.raises(ValueError, match="retries must be non-negative"):
            options.validate()

    def test_validate_prune_and_catalog_only(self) -> None:
        """Test validate with prune and catalog_only."""
        options = SyncOptions(
            game_version="2.19.1",
            resource_version="2.19.1@base64",
            output=Path("/tmp/output"),
            prune=True,
            catalog_only=True,
        )

        with pytest.raises(
            ValueError, match="prune cannot be combined with catalog_only"
        ):
            options.validate()

    def test_validate_dry_run_and_stats_only(self) -> None:
        """Test validate with dry_run and stats_only."""
        options = SyncOptions(
            game_version="2.19.1",
            resource_version="2.19.1@base64",
            output=Path("/tmp/output"),
            dry_run=True,
            stats_only=True,
        )

        with pytest.raises(
            ValueError, match="dry_run and stats_only are mutually exclusive"
        ):
            options.validate()

    def test_validate_dry_run_and_catalog_only(self) -> None:
        """Test validate with dry_run and catalog_only."""
        options = SyncOptions(
            game_version="2.19.1",
            resource_version="2.19.1@base64",
            output=Path("/tmp/output"),
            dry_run=True,
            catalog_only=True,
        )

        with pytest.raises(
            ValueError, match="dry_run cannot be combined with catalog_only"
        ):
            options.validate()

    def test_validate_stats_only_and_catalog_only(self) -> None:
        """Test validate with stats_only and catalog_only."""
        options = SyncOptions(
            game_version="2.19.1",
            resource_version="2.19.1@base64",
            output=Path("/tmp/output"),
            stats_only=True,
            catalog_only=True,
        )

        with pytest.raises(
            ValueError, match="stats_only cannot be combined with catalog_only"
        ):
            options.validate()


class TestSyncResult:
    """Tests for the ``sync_resources`` result dataclass."""

    def test_defaults(self) -> None:
        """A fresh result is empty and writes no snapshot."""
        result = SyncResult()

        assert result.assets == ()
        assert result.results == ()
        assert result.downloaded == 0
        assert result.skipped == 0
        assert result.failed == 0
        assert result.resumed == 0
        assert result.catalog_fingerprint == ""
        assert result.catalog_count == 0
        assert result.total_asset_count == 0
        assert result.state is None

    def test_counts_and_alignment(self) -> None:
        """Counters and per-asset results are stored as given."""
        entry = CatalogEntry(
            label="a",
            type_name="asset",
            size=4,
            checksum=1,
            seed=0,
            label_crc=2,
            priority=0,
            resource_type=ResourceType.RawPlain,
            resource_type_value=int(ResourceType.RawPlain),
        )
        asset = CatalogAsset(
            entry=entry,
            catalog_label="root",
            output_path="/tmp/a.asset",
            relative_path="root/a.asset",
            real_name="deadbeef",
        )
        boom = ValueError("nope")

        result = SyncResult(
            assets=(asset,),
            results=(boom,),
            downloaded=0,
            skipped=0,
            failed=1,
            resumed=0,
            catalog_fingerprint="f" * 64,
            catalog_count=1,
            total_asset_count=1,
            state={"status": "PARTIAL"},
        )

        assert result.assets == (asset,)
        assert result.results[0] is boom
        assert result.failed == 1
        assert result.catalog_fingerprint == "f" * 64
        assert result.state == {"status": "PARTIAL"}

    def test_state_is_optional_and_mutable(self) -> None:
        """``state`` starts unset and can be filled in after construction."""
        result = SyncResult()

        assert result.state is None
        result.state = {"status": "OK"}
        assert result.state == {"status": "OK"}
