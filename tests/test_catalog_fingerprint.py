"""Tests for the catalog fingerprint and the full discovery result."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from prism_sync import (
    catalog_fingerprint,
    discover_catalog_assets,
    discover_catalog_tree,
)
from prism_sync.crc import root_label_crc
from prism_sync.errors import CatalogTreeError
from prism_sync.models import (
    AssetInfoEntry,
    CatalogEntry,
    CatalogManifest,
    ResourceType,
)
from prism_sync.varint import encode_varint

GAME_VERSION = "1.0"
ROOT_SIMPLE = "test-simple"


# --------------------------------------------------------------------------
# Original algorithm, transcribed from PrismAutoSync.catalog_repository
# --------------------------------------------------------------------------


def _original_fingerprint(assetinfo: dict[str, list[dict[str, str]]]) -> str:
    """Reference implementation, copied verbatim from the original project."""
    payload = json.dumps(
        assetinfo,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def make_entry(
    label: str,
    *,
    type_name: str = "asset",
    size: int = 10,
    checksum: int | None = None,
    label_crc: int | None = None,
) -> CatalogEntry:
    return CatalogEntry(
        label=label,
        type_name=type_name,
        size=size,
        checksum=_digest(label, "c") if checksum is None else checksum,
        seed=0,
        label_crc=_digest(label, "l") if label_crc is None else label_crc,
        priority=0,
        resource_type=(None if type_name == "catalog" else ResourceType.RawPlain),
        resource_type_value=0 if type_name == "catalog" else int(ResourceType.RawPlain),
    )


def _digest(text: str, salt: str) -> int:
    raw = hashlib.sha256(f"{salt}:{text}".encode()).digest()[:6]
    return int.from_bytes(raw, "big") or 1


def subcatalog(label: str) -> CatalogEntry:
    return make_entry(label, type_name="catalog")


ROOT = CatalogManifest(
    label_crc=root_label_crc(GAME_VERSION, ROOT_SIMPLE),
    size=64,
    checksum=0x1111,
    seed=0x2222,
)
ROOT_KEY = ROOT.real_name()


def encode_version(manifest: CatalogManifest) -> str:
    payload = (
        manifest.checksum.to_bytes(8, "big")
        + encode_varint(manifest.size)
        + manifest.seed.to_bytes(8, "big")
    )
    return f"{ROOT_SIMPLE}@{base64.b64encode(payload).decode('ascii')}"


ROOT_VERSION = encode_version(ROOT)


class FakeCatalogLoader:
    def __init__(self, table: dict[str, list[CatalogEntry]]) -> None:
        self.table = table
        self.calls: list[str] = []

    def __call__(
        self,
        client: object,
        manifest: CatalogManifest,
        output: str | Path,
        *,
        use_cache: bool = True,
        refresh: bool = False,
        writable: bool = True,
    ) -> list[CatalogEntry]:
        real = manifest.real_name()
        self.calls.append(real)
        return list(self.table[real])


@pytest.fixture
def patched_loader(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(table: dict[str, list[CatalogEntry]]) -> FakeCatalogLoader:
        loader = FakeCatalogLoader(table)
        monkeypatch.setattr("prism_sync.catalog_client.load_catalog", loader)
        return loader

    return install


# --------------------------------------------------------------------------
# Fingerprint
# --------------------------------------------------------------------------


class TestFingerprintMatchesOriginal:
    """The digest must equal the original project's ``_fingerprint``."""

    def test_empty_tree(self) -> None:
        assert catalog_fingerprint({}) == _original_fingerprint({})

    def test_single_catalog(self) -> None:
        assetinfo = {
            "root": [
                AssetInfoEntry(name="a.asset", path="aa" * 16, type_name="RawPlain"),
                AssetInfoEntry(name="b.asset", path="bb" * 16, type_name="RawPlain"),
            ]
        }

        assert catalog_fingerprint(assetinfo) == _original_fingerprint(
            {
                "root": [
                    {"Name": "a.asset", "Path": "aa" * 16, "Type": "RawPlain"},
                    {"Name": "b.asset", "Path": "bb" * 16, "Type": "RawPlain"},
                ]
            }
        )

    def test_nested_catalogs(self) -> None:
        assetinfo = {
            "root": [AssetInfoEntry("sub.catalog", "cc" * 16, "catalog")],
            "sub": [AssetInfoEntry("x.asset", "dd" * 16, "RawPlain")],
        }
        expected_input = {
            "root": [{"Name": "sub.catalog", "Path": "cc" * 16, "Type": "catalog"}],
            "sub": [{"Name": "x.asset", "Path": "dd" * 16, "Type": "RawPlain"}],
        }

        assert catalog_fingerprint(assetinfo) == _original_fingerprint(expected_input)

    def test_non_ascii_is_not_escaped(self) -> None:
        assetinfo = {"root": [AssetInfoEntry("资源.asset", "ee" * 16, "RawPlain")]}
        expected_input = {
            "root": [{"Name": "资源.asset", "Path": "ee" * 16, "Type": "RawPlain"}]
        }

        assert catalog_fingerprint(assetinfo) == _original_fingerprint(expected_input)
        # A naive ``ensure_ascii=True`` digest would differ.
        assert (
            catalog_fingerprint(assetinfo)
            != hashlib.sha256(
                json.dumps(
                    expected_input,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )


class TestFingerprintStability:
    """Determinism and ordering semantics."""

    def test_same_input_same_digest(self) -> None:
        assetinfo = {"root": [AssetInfoEntry("a.asset", "aa" * 16, "RawPlain")]}

        assert catalog_fingerprint(assetinfo) == catalog_fingerprint(assetinfo)

    def test_label_order_does_not_matter(self) -> None:
        """``sort_keys=True`` sorts mapping keys, so insertion order is free."""
        first = {
            "b": [AssetInfoEntry("y.asset", "bb" * 16, "RawPlain")],
            "a": [AssetInfoEntry("x.asset", "aa" * 16, "RawPlain")],
        }
        second = {
            "a": [AssetInfoEntry("x.asset", "aa" * 16, "RawPlain")],
            "b": [AssetInfoEntry("y.asset", "bb" * 16, "RawPlain")],
        }

        assert catalog_fingerprint(first) == catalog_fingerprint(second)

    def test_entry_order_matters(self) -> None:
        """List elements are not sorted: reordering changes the digest."""
        forward = {
            "root": [
                AssetInfoEntry("a.asset", "aa" * 16, "RawPlain"),
                AssetInfoEntry("b.asset", "bb" * 16, "RawPlain"),
            ]
        }
        reverse = {
            "root": [
                AssetInfoEntry("b.asset", "bb" * 16, "RawPlain"),
                AssetInfoEntry("a.asset", "aa" * 16, "RawPlain"),
            ]
        }

        assert catalog_fingerprint(forward) != catalog_fingerprint(reverse)
        assert catalog_fingerprint(reverse) == _original_fingerprint(
            {
                "root": [
                    {"Name": "b.asset", "Path": "bb" * 16, "Type": "RawPlain"},
                    {"Name": "a.asset", "Path": "aa" * 16, "Type": "RawPlain"},
                ]
            }
        )

    def test_digest_is_lowercase_sha256(self) -> None:
        digest = catalog_fingerprint({})

        assert len(digest) == 64
        assert digest == digest.lower()
        int(digest, 16)  # raises if not hex


# --------------------------------------------------------------------------
# discover_catalog_tree
# --------------------------------------------------------------------------


class TestDiscoverCatalogTree:
    """The full result must expose both assets and assetinfo."""

    def test_returns_assets_and_assetinfo(
        self, tmp_path: Path, patched_loader: Any
    ) -> None:
        patched_loader({ROOT_KEY: [make_entry("a")]})

        assets, assetinfo = discover_catalog_tree(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
        )

        assert [item.entry.label for item in assets] == ["a"]
        assert list(assetinfo) == ["root"]
        assert [entry.name for entry in assetinfo["root"]] == ["a.asset"]

    def test_subcatalog_is_in_assetinfo_but_not_assets(
        self, tmp_path: Path, patched_loader: Any
    ) -> None:
        child = subcatalog("sub")
        patched_loader(
            {
                ROOT_KEY: [make_entry("a"), child],
                child.as_manifest().real_name(): [make_entry("x")],
            }
        )

        assets, assetinfo = discover_catalog_tree(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
        )

        assert [item.entry.label for item in assets] == ["a", "x"]
        assert [entry.name for entry in assetinfo["root"]] == ["a.asset", "sub.catalog"]
        assert [entry.name for entry in assetinfo["sub"]] == ["x.asset"]
        # ``AssetInfoEntry.type_name`` records ``resource_type_name``, which is
        # ``Unknown(<value>)`` for a catalog entry because its numeric resource
        # type is not one of the known asset types.  This matches the original
        # project exactly; the ``.catalog`` suffix comes from ``entry.type_name``.
        assert assetinfo["root"][1].type_name == "Unknown(0)"
        assert assetinfo["root"][1].name == "sub.catalog"

    def test_assetinfo_entry_fields(self, tmp_path: Path, patched_loader: Any) -> None:
        entry = make_entry("a", size=17, checksum=0xABCD, label_crc=0x77)
        patched_loader({ROOT_KEY: [entry]})

        _assets, assetinfo = discover_catalog_tree(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
        )

        info = assetinfo["root"][0]
        assert info.name == "a.asset"
        assert info.type_name == "RawPlain"
        assert info.path == entry.as_manifest().real_name()
        assert info.to_dict() == {
            "Name": "a.asset",
            "Path": entry.as_manifest().real_name(),
            "Type": "RawPlain",
        }

    def test_two_levels(self, tmp_path: Path, patched_loader: Any) -> None:
        a = subcatalog("A")
        b = subcatalog("B")
        patched_loader(
            {
                ROOT_KEY: [a],
                a.as_manifest().real_name(): [b],
                b.as_manifest().real_name(): [make_entry("x")],
            }
        )

        assets, assetinfo = discover_catalog_tree(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
        )

        assert assets[0].relative_path == "root/A/B/x.asset"
        assert list(assetinfo) == ["root", "A", "B"]

    def test_order_matches_assets_api(
        self, tmp_path: Path, patched_loader: Any
    ) -> None:
        first = subcatalog("first")
        second = subcatalog("second")
        table = {
            ROOT_KEY: [make_entry("r0"), first, second],
            first.as_manifest().real_name(): [make_entry("f1")],
            second.as_manifest().real_name(): [make_entry("s1")],
        }
        loader = patched_loader(table)

        assets, assetinfo = discover_catalog_tree(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
        )
        loader.calls.clear()
        assets_via_old_api = discover_catalog_assets(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
        )

        assert [a.entry.label for a in assets] == ["r0", "f1", "s1"]
        assert [a.relative_path for a in assets] == [
            a.relative_path for a in assets_via_old_api
        ]
        assert list(assetinfo) == ["root", "first", "second"]

    def test_fingerprint_from_tree_matches_original(
        self, tmp_path: Path, patched_loader: Any
    ) -> None:
        child = subcatalog("sub")
        patched_loader(
            {
                ROOT_KEY: [make_entry("a"), child],
                child.as_manifest().real_name(): [make_entry("x")],
            }
        )

        _assets, assetinfo = discover_catalog_tree(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
        )

        as_plain = {
            label: [entry.to_dict() for entry in entries]
            for label, entries in assetinfo.items()
        }
        assert catalog_fingerprint(assetinfo) == _original_fingerprint(as_plain)

    def test_deterministic_fingerprint_across_runs(
        self, tmp_path: Path, patched_loader: Any
    ) -> None:
        child = subcatalog("sub")
        table = {
            ROOT_KEY: [make_entry("a"), child],
            child.as_manifest().real_name(): [make_entry("x")],
        }
        patched_loader(table)

        _a1, info1 = discover_catalog_tree(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
        )
        _a2, info2 = discover_catalog_tree(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
        )

        assert catalog_fingerprint(info1) == catalog_fingerprint(info2)

    def test_error_propagates_without_partial_result(
        self, tmp_path: Path, patched_loader: Any
    ) -> None:
        child = subcatalog("missing")
        patched_loader({ROOT_KEY: [make_entry("a"), child]})

        with pytest.raises(KeyError):
            discover_catalog_tree(
                object(),  # type: ignore[arg-type]
                ROOT_VERSION,
                game_version=GAME_VERSION,
                output=tmp_path,
            )

    def test_duplicate_label_still_rejected(
        self, tmp_path: Path, patched_loader: Any
    ) -> None:
        twin_a = make_entry("twin", type_name="catalog", checksum=11, label_crc=11)
        twin_b = make_entry("twin", type_name="catalog", checksum=22, label_crc=22)
        parent = subcatalog("parent")
        patched_loader(
            {
                ROOT_KEY: [twin_a],
                twin_a.as_manifest().real_name(): [parent],
                parent.as_manifest().real_name(): [twin_b],
                twin_b.as_manifest().real_name(): [make_entry("x")],
            }
        )

        with pytest.raises(CatalogTreeError, match="duplicate catalog label"):
            discover_catalog_tree(
                object(),  # type: ignore[arg-type]
                ROOT_VERSION,
                game_version=GAME_VERSION,
                output=tmp_path,
            )

    def test_empty_catalog_yields_empty_assetinfo_entry(
        self, tmp_path: Path, patched_loader: Any
    ) -> None:
        """A catalog with no entries is still recorded, with an empty list."""
        patched_loader({ROOT_KEY: []})

        assets, assetinfo = discover_catalog_tree(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
        )

        assert assets == []
        assert assetinfo == {"root": []}


# --------------------------------------------------------------------------
# discover_catalog_assets must not regress
# --------------------------------------------------------------------------


class TestDiscoverAssetsUnchanged:
    """The 5b-3 surface keeps its contract after the refactor."""

    def test_returns_only_assets(self, tmp_path: Path, patched_loader: Any) -> None:
        child = subcatalog("sub")
        patched_loader(
            {
                ROOT_KEY: [make_entry("a"), child],
                child.as_manifest().real_name(): [make_entry("x")],
            }
        )

        assets = discover_catalog_assets(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
        )

        assert [item.entry.label for item in assets] == ["a", "x"]

    def test_defaults_still_forwarded(
        self, tmp_path: Path, patched_loader: Any
    ) -> None:
        loader = patched_loader({ROOT_KEY: [make_entry("a")]})

        discover_catalog_assets(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
            use_cache=False,
            refresh=True,
            writable=False,
        )

        assert loader.calls == [ROOT_KEY]
