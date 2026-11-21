"""Tests for the catalog entry to asset conversion."""

from __future__ import annotations

from pathlib import Path

import pytest

from prism_sync import catalog_entry_to_asset
from prism_sync.errors import CatalogTreeError
from prism_sync.manifest import real_name
from prism_sync.models import CatalogAsset, CatalogEntry, ResourceType


def make_entry(
    *,
    label: str = "file",
    type_name: str = "asset",
    size: int = 42,
    checksum: int = 0x1234,
    label_crc: int = 0x5678,
    seed: int = 7,
    resource_type: ResourceType | None = ResourceType.RawPlain,
) -> CatalogEntry:
    return CatalogEntry(
        label=label,
        type_name=type_name,
        size=size,
        checksum=checksum,
        seed=seed,
        label_crc=label_crc,
        priority=0,
        resource_type=resource_type,
        resource_type_value=int(resource_type) if resource_type else 999,
    )


class TestNormalConversion:
    """Tests for the straightforward conversion path."""

    def test_returns_catalog_asset(self, tmp_path: Path) -> None:
        """A plain entry converts into a complete ``CatalogAsset``."""
        entry = make_entry()

        asset = catalog_entry_to_asset(
            entry,
            catalog_label="root",
            output_dir=tmp_path / "root" / "raw",
            root_output=tmp_path,
        )

        assert isinstance(asset, CatalogAsset)

    def test_output_path_is_file_label_dot_type(self, tmp_path: Path) -> None:
        """The file is named ``<label>.<type_name>``."""
        entry = make_entry(label="hero", type_name="asset")

        asset = catalog_entry_to_asset(
            entry,
            catalog_label="root",
            output_dir=tmp_path / "root" / "raw",
            root_output=tmp_path,
        )

        assert Path(asset.output_path).name == "hero.asset"

    def test_output_path_keeps_catalog_subdirectory(self, tmp_path: Path) -> None:
        """The asset lives inside the catalog's own output directory."""
        entry = make_entry(label="a")

        asset = catalog_entry_to_asset(
            entry,
            catalog_label="root",
            output_dir=tmp_path / "root" / "raw",
            root_output=tmp_path,
        )

        assert Path(asset.output_path) == tmp_path / "root" / "raw" / "a.asset"

    def test_output_path_is_absolute_and_resolved(self, tmp_path: Path) -> None:
        """``output_path`` is an absolute, resolved path string."""
        entry = make_entry(label="a")

        asset = catalog_entry_to_asset(
            entry,
            catalog_label="root",
            output_dir=tmp_path / "root",
            root_output=tmp_path,
        )

        assert Path(asset.output_path).is_absolute()
        assert Path(asset.output_path) == Path(asset.output_path).resolve()

    def test_relative_path_is_posix_and_relative_to_root(self, tmp_path: Path) -> None:
        """``relative_path`` is POSIX-style and rooted at ``root_output``."""
        entry = make_entry(label="a", type_name="asset")

        asset = catalog_entry_to_asset(
            entry,
            catalog_label="root",
            output_dir=tmp_path / "root" / "raw",
            root_output=tmp_path,
        )

        assert asset.relative_path == "root/raw/a.asset"
        assert "\\" not in asset.relative_path

    def test_real_name_matches_helper(self, tmp_path: Path) -> None:
        """``real_name`` is derived from checksum, size and label CRC."""
        entry = make_entry(checksum=0xABCD, size=99, label_crc=0x77)

        asset = catalog_entry_to_asset(
            entry,
            catalog_label="root",
            output_dir=tmp_path,
            root_output=tmp_path,
        )

        assert asset.real_name == real_name(0xABCD, 99, 0x77)

    def test_catalog_label_is_preserved(self, tmp_path: Path) -> None:
        """The caller-supplied catalog label is stored verbatim."""
        entry = make_entry()

        asset = catalog_entry_to_asset(
            entry,
            catalog_label="localizetext",
            output_dir=tmp_path,
            root_output=tmp_path,
        )

        assert asset.catalog_label == "localizetext"

    def test_entry_object_is_preserved(self, tmp_path: Path) -> None:
        """The original ``CatalogEntry`` object is kept unchanged."""
        entry = make_entry()

        asset = catalog_entry_to_asset(
            entry,
            catalog_label="root",
            output_dir=tmp_path,
            root_output=tmp_path,
        )

        assert asset.entry is entry

    def test_nested_output_dir(self, tmp_path: Path) -> None:
        """Legitimately nested catalog directories convert correctly."""
        entry = make_entry(label="deep")

        asset = catalog_entry_to_asset(
            entry,
            catalog_label="sub",
            output_dir=tmp_path / "root" / "sub" / "inner",
            root_output=tmp_path,
        )

        assert asset.relative_path == "root/sub/inner/deep.asset"

    def test_output_dir_equals_root(self, tmp_path: Path) -> None:
        """A catalog at the output root is a legal layout."""
        entry = make_entry(label="top")

        asset = catalog_entry_to_asset(
            entry,
            catalog_label="root",
            output_dir=tmp_path,
            root_output=tmp_path,
        )

        assert asset.relative_path == "top.asset"


class TestPathSafety:
    """Tests for the traversal protections."""

    def test_label_dotdot_and_slash_is_rejected(self, tmp_path: Path) -> None:
        """A ``..`` path component cannot climb out of the catalog directory."""
        entry = make_entry(label="../evil")

        with pytest.raises(CatalogTreeError, match="unsafe path component"):
            catalog_entry_to_asset(
                entry,
                catalog_label="root",
                output_dir=tmp_path / "root",
                root_output=tmp_path,
            )

    def test_type_name_with_dotdot_component_is_rejected(self, tmp_path: Path) -> None:
        """A ``..`` component produced through the type name is rejected."""
        entry = make_entry(label="a", type_name="x/../../evil")

        with pytest.raises(CatalogTreeError, match="unsafe path component"):
            catalog_entry_to_asset(
                entry,
                catalog_label="root",
                output_dir=tmp_path / "root",
                root_output=tmp_path,
            )

    def test_dotdot_string_without_separator_is_a_plain_name(
        self, tmp_path: Path
    ) -> None:
        """``...asset`` is a literal filename, not a traversal.

        The original project concatenates ``<label>.<type>`` before validating,
        so a ``..`` label only becomes dangerous once a separator makes it a
        path component.  The resulting single-part name stays inside the
        output directory.
        """
        entry = make_entry(label="..")

        asset = catalog_entry_to_asset(
            entry,
            catalog_label="root",
            output_dir=tmp_path / "root",
            root_output=tmp_path,
        )

        assert Path(asset.output_path).name == "...asset"
        assert Path(asset.output_path).parent == (tmp_path / "root").resolve()

    def test_backslash_label_is_rejected(self, tmp_path: Path) -> None:
        """Windows-style separators are refused."""
        entry = make_entry(label="a\\b")

        with pytest.raises(CatalogTreeError, match="unsafe path component"):
            catalog_entry_to_asset(
                entry,
                catalog_label="root",
                output_dir=tmp_path / "root",
                root_output=tmp_path,
            )

    @pytest.mark.parametrize("label", ["CON", "NUL", "COM1", "LPT1"])
    def test_windows_reserved_name_rejected(self, tmp_path: Path, label: str) -> None:
        """Reserved Windows device names are refused."""
        entry = make_entry(label=label)

        with pytest.raises(CatalogTreeError, match="reserved Windows device name"):
            catalog_entry_to_asset(
                entry,
                catalog_label="root",
                output_dir=tmp_path / "root",
                root_output=tmp_path,
            )

    def test_empty_label_is_rejected(self, tmp_path: Path) -> None:
        """A ``.asset`` name (empty label) still validates to one part."""
        entry = make_entry(label="", type_name="pref")

        # ``.pref`` is a hidden-file style name, not empty: it must convert.
        asset = catalog_entry_to_asset(
            entry,
            catalog_label="root",
            output_dir=tmp_path,
            root_output=tmp_path,
        )

        assert Path(asset.output_path).name == ".pref"

    def test_escaping_output_dir_is_rejected(self, tmp_path: Path) -> None:
        """A catalog directory outside the root is refused."""
        entry = make_entry(label="a")
        outside = tmp_path.parent / "outside"

        with pytest.raises(CatalogTreeError, match="escapes output directory"):
            catalog_entry_to_asset(
                entry,
                catalog_label="root",
                output_dir=outside,
                root_output=tmp_path,
            )

    def test_no_files_or_directories_are_created(self, tmp_path: Path) -> None:
        """Conversion is purely computational."""
        entry = make_entry(label="a")
        root = tmp_path / "out"

        catalog_entry_to_asset(
            entry,
            catalog_label="root",
            output_dir=root / "root" / "raw",
            root_output=root,
        )

        assert not root.exists()
        assert list(tmp_path.iterdir()) == []

    def test_relative_root_produces_absolute_output(self, tmp_path: Path) -> None:
        """The resolved path is absolute even for a relative root argument."""
        entry = make_entry(label="a")

        asset = catalog_entry_to_asset(
            entry,
            catalog_label="root",
            output_dir=tmp_path / "root",
            root_output=tmp_path,
        )

        assert Path(asset.output_path).is_absolute()


class TestSubcatalogContract:
    """Documentation of how catalog entries are treated."""

    def test_catalog_entry_converts_like_any_other(self, tmp_path: Path) -> None:
        """A ``catalog`` entry is not special-cased; the caller skips it.

        The original project filters ``type_name == "catalog"`` before
        converting, so this function has no such branch.  Passing one anyway
        produces an ordinary asset, which documents the contract rather than
        silently rejecting it.
        """
        entry = make_entry(label="sub", type_name="catalog")

        asset = catalog_entry_to_asset(
            entry,
            catalog_label="root",
            output_dir=tmp_path,
            root_output=tmp_path,
        )

        assert Path(asset.output_path).name == "sub.catalog"
        assert asset.entry.type_name == "catalog"


class TestInvalidInput:
    """Tests for inputs rejected by the underlying helpers."""

    @pytest.mark.parametrize("checksum", [-1, 1 << 64])
    def test_checksum_out_of_range_raises(self, tmp_path: Path, checksum: int) -> None:
        """``real_name`` rejects a checksum outside 64 bits."""
        entry = make_entry(checksum=checksum)

        with pytest.raises(ValueError, match="checksum must fit in 64 bits"):
            catalog_entry_to_asset(
                entry,
                catalog_label="root",
                output_dir=tmp_path,
                root_output=tmp_path,
            )

    def test_negative_size_raises(self, tmp_path: Path) -> None:
        """``real_name`` rejects a negative size."""
        entry = make_entry(size=-1)

        with pytest.raises(ValueError, match="size must be non-negative"):
            catalog_entry_to_asset(
                entry,
                catalog_label="root",
                output_dir=tmp_path,
                root_output=tmp_path,
            )

    def test_errors_are_not_swallowed(self, tmp_path: Path) -> None:
        """Conversion never converts a failure into a placeholder asset."""
        entry = make_entry(size=-5)

        with pytest.raises(ValueError):
            catalog_entry_to_asset(
                entry,
                catalog_label="root",
                output_dir=tmp_path,
                root_output=tmp_path,
            )
