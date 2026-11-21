"""Tests for asset selection."""

from __future__ import annotations

import pytest

from prism_sync.models import CatalogAsset, CatalogEntry, ResourceType
from prism_sync.selection import (
    PlanTypeStat,
    Selection,
    SyncPlan,
    TypeStat,
    parse_size,
    parse_types,
)


class TestParseSize:
    """Tests for parse_size function."""

    def test_bytes(self) -> None:
        """Test parsing bytes."""
        assert parse_size("1024") == 1024
        assert parse_size("1024B") == 1024
        assert parse_size("1024 B") == 1024

    def test_kilobytes(self) -> None:
        """Test parsing kilobytes."""
        assert parse_size("1KB") == 1024
        assert parse_size("1 KB") == 1024
        assert parse_size("1K") == 1024
        assert parse_size("10KB") == 10 * 1024

    def test_megabytes(self) -> None:
        """Test parsing megabytes."""
        assert parse_size("1MB") == 1024 * 1024
        assert parse_size("1 MB") == 1024 * 1024
        assert parse_size("1M") == 1024 * 1024
        assert parse_size("1.5MB") == int(1.5 * 1024 * 1024)

    def test_gigabytes(self) -> None:
        """Test parsing gigabytes."""
        assert parse_size("1GB") == 1024 * 1024 * 1024
        assert parse_size("1 GB") == 1024 * 1024 * 1024
        assert parse_size("1G") == 1024 * 1024 * 1024

    def test_terabytes(self) -> None:
        """Test parsing terabytes."""
        assert parse_size("1TB") == 1024 * 1024 * 1024 * 1024
        assert parse_size("1 TB") == 1024 * 1024 * 1024 * 1024
        assert parse_size("1T") == 1024 * 1024 * 1024 * 1024

    def test_case_insensitive(self) -> None:
        """Test case insensitive parsing."""
        assert parse_size("1kb") == 1024
        assert parse_size("1Kb") == 1024
        assert parse_size("1kB") == 1024

    def test_invalid_raises(self) -> None:
        """Test invalid size raises ValueError."""
        with pytest.raises(ValueError, match="invalid size"):
            parse_size("invalid")

    def test_invalid_unit_raises(self) -> None:
        """Test invalid unit raises ValueError."""
        with pytest.raises(ValueError, match="invalid size"):
            parse_size("10XX")


class TestParseTypes:
    """Tests for parse_types function."""

    def test_single_type(self) -> None:
        """Test parsing single type."""
        result = parse_types("json")
        assert result == frozenset({"json"})

    def test_multiple_types(self) -> None:
        """Test parsing multiple types."""
        result = parse_types("json,bytes,acb")
        assert result == frozenset({"json", "bytes", "acb"})

    def test_with_spaces(self) -> None:
        """Test parsing with spaces."""
        result = parse_types("json, bytes , acb")
        assert result == frozenset({"json", "bytes", "acb"})

    def test_case_normalization(self) -> None:
        """Test case normalization."""
        result = parse_types("JSON,Bytes,ACB")
        assert result == frozenset({"json", "bytes", "acb"})

    def test_empty_raises(self) -> None:
        """Test empty types raises ValueError."""
        with pytest.raises(ValueError, match="types must contain at least one value"):
            parse_types("")

    def test_whitespace_only_raises(self) -> None:
        """Test whitespace only raises ValueError."""
        with pytest.raises(ValueError, match="types must contain at least one value"):
            parse_types("   ")


class TestSelection:
    """Tests for Selection dataclass."""

    def test_creation(self) -> None:
        """Test Selection creation."""
        sel = Selection()
        assert sel.types is None
        assert sel.include == ()
        assert sel.exclude == ()
        assert sel.max_size is None
        assert sel.active is False

    def test_with_types(self) -> None:
        """Test Selection with types."""
        sel = Selection(types=frozenset({"json", "bytes"}))
        assert sel.types == frozenset({"json", "bytes"})
        assert sel.active is True

    def test_types_normalization(self) -> None:
        """Test types normalization."""
        sel = Selection(types=frozenset({"JSON", "Bytes", "  json  "}))
        assert sel.types == frozenset({"json", "bytes"})

    def test_with_include(self) -> None:
        """Test Selection with include patterns."""
        sel = Selection(include=("*.json", "*.bytes"))
        assert sel.include == ("*.bytes", "*.json")  # Sorted
        assert sel.active is True

    def test_with_exclude(self) -> None:
        """Test Selection with exclude patterns."""
        sel = Selection(exclude=("*.json", "*.bytes"))
        assert sel.exclude == ("*.bytes", "*.json")  # Sorted
        assert sel.active is True

    def test_with_max_size(self) -> None:
        """Test Selection with max_size."""
        sel = Selection(max_size=1024)
        assert sel.max_size == 1024
        assert sel.active is True

    def test_negative_max_size_raises(self) -> None:
        """Test negative max_size raises ValueError."""
        with pytest.raises(ValueError, match="max_size must be non-negative"):
            Selection(max_size=-1)

    def test_to_dict(self) -> None:
        """Test to_dict method."""
        sel = Selection(
            types=frozenset({"json", "bytes"}),
            include=("*.json",),
            exclude=("*.tmp",),
            max_size=1024,
        )
        result = sel.to_dict()
        assert result["active"] is True
        assert result["types"] == ["bytes", "json"]
        assert result["include"] == ["*.json"]
        assert result["exclude"] == ["*.tmp"]
        assert result["maxSize"] == 1024

    def test_frozen(self) -> None:
        """Test that Selection is frozen."""
        sel = Selection()
        with pytest.raises(AttributeError):
            sel.types = frozenset({"json"})  # type: ignore


class TestSelectionMatches:
    """Tests for Selection.matches method."""

    def _create_asset(
        self,
        label: str = "test",
        type_name: str = "json",
        size: int = 100,
        relative_path: str = "root/test.json",
    ) -> CatalogAsset:
        """Create a test CatalogAsset."""
        entry = CatalogEntry(
            label=label,
            type_name=type_name,
            size=size,
            checksum=0,
            seed=0,
            label_crc=0,
            priority=0,
            resource_type=ResourceType.RawPlain,
            resource_type_value=64,
        )
        return CatalogAsset(
            entry=entry,
            catalog_label="root",
            output_path="root/test.json",
            relative_path=relative_path,
            real_name="test",
        )

    def test_no_filters(self) -> None:
        """Test with no filters (matches all)."""
        sel = Selection()
        asset = self._create_asset()
        assert sel.matches(asset) is True

    def test_types_filter_match(self) -> None:
        """Test types filter with matching type."""
        sel = Selection(types=frozenset({"json"}))
        asset = self._create_asset(type_name="json")
        assert sel.matches(asset) is True

    def test_types_filter_no_match(self) -> None:
        """Test types filter with non-matching type."""
        sel = Selection(types=frozenset({"json"}))
        asset = self._create_asset(type_name="bytes")
        assert sel.matches(asset) is False

    def test_max_size_filter_match(self) -> None:
        """Test max_size filter with matching size."""
        sel = Selection(max_size=1024)
        asset = self._create_asset(size=100)
        assert sel.matches(asset) is True

    def test_max_size_filter_no_match(self) -> None:
        """Test max_size filter with non-matching size."""
        sel = Selection(max_size=100)
        asset = self._create_asset(size=1024)
        assert sel.matches(asset) is False

    def test_include_filter_match(self) -> None:
        """Test include filter with matching pattern."""
        sel = Selection(include=("*.json",))
        asset = self._create_asset(relative_path="root/test.json")
        assert sel.matches(asset) is True

    def test_include_filter_no_match(self) -> None:
        """Test include filter with non-matching pattern."""
        sel = Selection(include=("*.json",))
        asset = self._create_asset(
            label="test", type_name="bytes", relative_path="root/test.bytes"
        )
        assert sel.matches(asset) is False

    def test_exclude_filter_match(self) -> None:
        """Test exclude filter with non-matching pattern."""
        sel = Selection(exclude=("*.tmp",))
        asset = self._create_asset(relative_path="root/test.json")
        assert sel.matches(asset) is True

    def test_exclude_filter_no_match(self) -> None:
        """Test exclude filter with matching pattern."""
        sel = Selection(exclude=("*.tmp",))
        asset = self._create_asset(relative_path="root/test.tmp")
        assert sel.matches(asset) is False

    def test_exclude_wins_over_include(self) -> None:
        """Test that exclude wins over include."""
        sel = Selection(include=("*.json",), exclude=("test.json",))
        asset = self._create_asset(relative_path="root/test.json")
        assert sel.matches(asset) is False


class TestTypeStat:
    """Tests for TypeStat dataclass."""

    def test_creation(self) -> None:
        """Test TypeStat creation."""
        stat = TypeStat(
            type_name="json",
            count=10,
            bytes=1024,
            selected_count=5,
            selected_bytes=512,
        )
        assert stat.type_name == "json"
        assert stat.count == 10
        assert stat.bytes == 1024
        assert stat.selected_count == 5
        assert stat.selected_bytes == 512

    def test_to_dict(self) -> None:
        """Test to_dict method."""
        stat = TypeStat(
            type_name="json",
            count=10,
            bytes=1024,
            selected_count=5,
            selected_bytes=512,
        )
        result = stat.to_dict()
        assert result["type"] == "json"
        assert result["count"] == 10
        assert result["bytes"] == 1024
        assert result["selectedCount"] == 5
        assert result["selectedBytes"] == 512


class TestPlanTypeStat:
    """Tests for PlanTypeStat dataclass."""

    def test_creation(self) -> None:
        """Test PlanTypeStat creation."""
        stat = PlanTypeStat(
            type_name="json",
            added=5,
            added_bytes=512,
            changed=3,
            changed_bytes=256,
            unchanged=2,
            unchanged_bytes=128,
        )
        assert stat.type_name == "json"
        assert stat.added == 5
        assert stat.added_bytes == 512
        assert stat.changed == 3
        assert stat.changed_bytes == 256
        assert stat.unchanged == 2
        assert stat.unchanged_bytes == 128

    def test_download_count(self) -> None:
        """Test download_count property."""
        stat = PlanTypeStat(
            type_name="json",
            added=5,
            added_bytes=512,
            changed=3,
            changed_bytes=256,
            unchanged=2,
            unchanged_bytes=128,
        )
        assert stat.download_count == 8

    def test_download_bytes(self) -> None:
        """Test download_bytes property."""
        stat = PlanTypeStat(
            type_name="json",
            added=5,
            added_bytes=512,
            changed=3,
            changed_bytes=256,
            unchanged=2,
            unchanged_bytes=128,
        )
        assert stat.download_bytes == 768

    def test_to_dict(self) -> None:
        """Test to_dict method."""
        stat = PlanTypeStat(
            type_name="json",
            added=5,
            added_bytes=512,
            changed=3,
            changed_bytes=256,
            unchanged=2,
            unchanged_bytes=128,
        )
        result = stat.to_dict()
        assert result["type"] == "json"
        assert result["added"] == 5
        assert result["addedBytes"] == 512
        assert result["changed"] == 3
        assert result["changedBytes"] == 256
        assert result["download"] == 8
        assert result["downloadBytes"] == 768


class TestSyncPlan:
    """Tests for SyncPlan dataclass."""

    def test_creation(self) -> None:
        """Test SyncPlan creation."""
        plan = SyncPlan(
            selection={"types": ["json"]},
            total_count=100,
            total_bytes=102400,
            selected_count=50,
            selected_bytes=51200,
            added_count=10,
            added_bytes=10240,
            changed_count=5,
            changed_bytes=5120,
            unchanged_count=35,
            unchanged_bytes=35840,
            removed_count=0,
            removed_bytes=0,
            free_bytes=1000000,
            required_bytes=15360,
            enough_space=True,
            by_type=[],
        )
        assert plan.total_count == 100
        assert plan.selected_count == 50
        assert plan.added_count == 10
        assert plan.changed_count == 5

    def test_to_dict(self) -> None:
        """Test to_dict method."""
        plan = SyncPlan(
            selection={"types": ["json"]},
            total_count=100,
            total_bytes=102400,
            selected_count=50,
            selected_bytes=51200,
            added_count=10,
            added_bytes=10240,
            changed_count=5,
            changed_bytes=5120,
            unchanged_count=35,
            unchanged_bytes=35840,
            removed_count=0,
            removed_bytes=0,
            free_bytes=1000000,
            required_bytes=15360,
            enough_space=True,
            by_type=[],
        )
        result = plan.to_dict()
        assert result["totalCount"] == 100
        assert result["selectedCount"] == 50
        assert result["addedCount"] == 10
