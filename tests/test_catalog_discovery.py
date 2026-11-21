"""Tests for recursive catalog asset discovery."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from prism_sync import discover_catalog_assets
from prism_sync.errors import CatalogTreeError
from prism_sync.http_client import DownloadError
from prism_sync.models import CatalogEntry, CatalogManifest, ResourceType

# --------------------------------------------------------------------------
# Synthetic catalog tree helpers
# --------------------------------------------------------------------------


def make_entry(
    label: str,
    *,
    type_name: str = "asset",
    size: int = 10,
    checksum: int | None = None,
    label_crc: int | None = None,
) -> CatalogEntry:
    """Build a catalog entry with deterministic checksum/label CRC."""
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
    """Return a stable non-zero 64-bit-ish integer for ``text``."""
    raw = hashlib.sha256(f"{salt}:{text}".encode()).digest()[:6]
    return int.from_bytes(raw, "big") or 1


def asset(label: str, **kwargs: Any) -> CatalogEntry:
    return make_entry(label, **kwargs)


def subcatalog(label: str) -> CatalogEntry:
    return make_entry(label, type_name="catalog")


class FakeCatalogLoader:
    """Replaces ``load_catalog`` with a table of pre-built entries."""

    def __init__(self, table: dict[str, list[CatalogEntry]]) -> None:
        self.table = table
        self.calls: list[tuple[str, bool, bool, bool]] = []

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
        self.calls.append((real, use_cache, refresh, writable))
        if real not in self.table:
            raise DownloadError(f"catalog {real} not in fixture table")
        return list(self.table[real])


def manifest_of(entry: CatalogEntry) -> CatalogManifest:
    return entry.as_manifest()


ROOT_SIMPLE = "test-simple"
GAME_VERSION = "1.0"


def root_manifest_for(
    checksum: int = 0x1111,
    size: int = 64,
    seed: int = 0x2222,
) -> CatalogManifest:
    """Build a root manifest the way ``discover_catalog_assets`` will."""
    from prism_sync.crc import root_label_crc

    return CatalogManifest(
        label_crc=root_label_crc(GAME_VERSION, ROOT_SIMPLE),
        size=size,
        checksum=checksum,
        seed=seed,
    )


def encode_version(manifest: CatalogManifest, simple: str = ROOT_SIMPLE) -> str:
    """Encode a manifest into the ``<simple>@<base64>`` resource version."""
    import base64

    from prism_sync.varint import encode_varint

    payload = (
        manifest.checksum.to_bytes(8, "big")
        + encode_varint(manifest.size)
        + manifest.seed.to_bytes(8, "big")
    )
    return f"{simple}@{base64.b64encode(payload).decode('ascii')}"


ROOT = root_manifest_for()
ROOT_VERSION = encode_version(ROOT)
ROOT_KEY = ROOT.real_name()


@pytest.fixture
def patched_loader(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a loader backed by a mutable table."""

    def install(table: dict[str, list[CatalogEntry]]) -> FakeCatalogLoader:
        loader = FakeCatalogLoader(table)
        monkeypatch.setattr("prism_sync.catalog_client.load_catalog", loader)
        return loader

    return install


def build_chain(depth: int) -> tuple[dict[str, list[CatalogEntry]], str]:
    """Build a root -> c1 -> ... -> c<depth> chain.

    Returns the loader table plus the label of the deepest catalog.  The root
    key is *not* populated so callers can decide what the root contains.
    """
    table: dict[str, list[CatalogEntry]] = {}
    previous: CatalogEntry | None = None
    for index in range(1, depth + 1):
        current = subcatalog(f"c{index}")
        if previous is None:
            # The root holds the first link; the caller fills the root key.
            table.setdefault("__root_children__", []).append(current)
        else:
            table[manifest_of(previous).real_name()] = [current]
        previous = current
    table[manifest_of(previous).real_name()] = []
    return table, previous.label


# --------------------------------------------------------------------------
# Basic discovery
# --------------------------------------------------------------------------


class TestBasicDiscovery:
    """Tests for the plain, non-recursive discovery paths."""

    def test_root_only(self, tmp_path: Path, patched_loader: Any) -> None:
        """A root catalog with plain assets yields those assets."""
        root = [asset("a"), asset("b")]
        patched_loader({ROOT_KEY: root})

        result = discover_catalog_assets(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
        )

        assert [item.entry.label for item in result] == ["a", "b"]

    def test_empty_root_catalog(self, tmp_path: Path, patched_loader: Any) -> None:
        """An empty catalog produces an empty list."""
        patched_loader({ROOT_KEY: []})

        result = discover_catalog_assets(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
        )

        assert result == []

    def test_only_subcatalog_not_returned(
        self, tmp_path: Path, patched_loader: Any
    ) -> None:
        """A catalog entry is never itself returned as an asset."""
        child = subcatalog("child")
        patched_loader(
            {
                ROOT_KEY: [child],
                manifest_of(child).real_name(): [asset("inner")],
            }
        )

        result = discover_catalog_assets(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
        )

        assert [item.entry.label for item in result] == ["inner"]
        assert all(item.entry.type_name != "catalog" for item in result)

    def test_root_mixed_with_subcatalog(
        self, tmp_path: Path, patched_loader: Any
    ) -> None:
        """Plain assets and sub-catalogs coexist in one catalog."""
        child = subcatalog("child")
        patched_loader(
            {
                ROOT_KEY: [asset("a"), child, asset("b")],
                manifest_of(child).real_name(): [asset("c")],
            }
        )

        result = discover_catalog_assets(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
        )

        labelled = [(item.catalog_label, item.entry.label) for item in result]
        assert labelled == [("root", "a"), ("root", "b"), ("child", "c")]


class TestNesting:
    """Tests for multi-level recursion."""

    def test_one_level(self, tmp_path: Path, patched_loader: Any) -> None:
        """One level of nesting lands under ``root/<child>``."""
        child = subcatalog("A")
        patched_loader(
            {
                ROOT_KEY: [child],
                manifest_of(child).real_name(): [asset("x")],
            }
        )

        result = discover_catalog_assets(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
        )

        assert result[0].relative_path == "root/A/x.asset"
        assert result[0].catalog_label == "A"

    def test_two_levels(self, tmp_path: Path, patched_loader: Any) -> None:
        """Two levels nest as ``root/A/B/x.asset``."""
        a = subcatalog("A")
        b = subcatalog("B")
        patched_loader(
            {
                ROOT_KEY: [a],
                manifest_of(a).real_name(): [b],
                manifest_of(b).real_name(): [asset("x")],
            }
        )

        result = discover_catalog_assets(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
        )

        assert result[0].relative_path == "root/A/B/x.asset"
        assert result[0].catalog_label == "B"

    def test_output_path_is_absolute(self, tmp_path: Path, patched_loader: Any) -> None:
        """``output_path`` is absolute and rooted at the resolved output."""
        patched_loader({ROOT_KEY: [asset("a")]})

        result = discover_catalog_assets(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
        )

        assert Path(result[0].output_path) == tmp_path.resolve() / "root" / "a.asset"

    def test_relative_output_is_resolved(
        self, tmp_path: Path, patched_loader: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relative or ``..``-containing output is resolved up front."""
        patched_loader({ROOT_KEY: [asset("a")]})
        monkeypatch.chdir(tmp_path)
        (tmp_path / "nested").mkdir()

        result = discover_catalog_assets(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=Path("nested") / ".." / "nested",
        )

        expected = (tmp_path / "nested").resolve() / "root" / "a.asset"
        assert Path(result[0].output_path) == expected


class TestOrdering:
    """Ordering must match the original depth-first walk."""

    def test_sibling_catalogs_keep_original_order(
        self, tmp_path: Path, patched_loader: Any
    ) -> None:
        """Sub-catalogs are expanded in the order they appear."""
        first = subcatalog("first")
        second = subcatalog("second")
        patched_loader(
            {
                ROOT_KEY: [first, second],
                manifest_of(first).real_name(): [asset("f1")],
                manifest_of(second).real_name(): [asset("s1")],
            }
        )

        result = discover_catalog_assets(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
        )

        assert [item.entry.label for item in result] == ["f1", "s1"]

    def test_depth_first_before_sibling(
        self, tmp_path: Path, patched_loader: Any
    ) -> None:
        """The whole first subtree is walked before the second sibling."""
        first = subcatalog("first")
        second = subcatalog("second")
        inner = subcatalog("inner")
        patched_loader(
            {
                ROOT_KEY: [asset("r0"), first, second],
                manifest_of(first).real_name(): [asset("f1"), inner],
                manifest_of(inner).real_name(): [asset("i1")],
                manifest_of(second).real_name(): [asset("s1")],
            }
        )

        result = discover_catalog_assets(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
        )

        assert [item.entry.label for item in result] == ["r0", "f1", "i1", "s1"]


class TestCycleAndDuplicateProtection:
    """Cycle, duplicate-label and depth guards."""

    def test_self_reference(self, tmp_path: Path, patched_loader: Any) -> None:
        """A catalog whose child points back at the root is rejected."""
        self_ref = make_entry(
            "loop",
            type_name="catalog",
            checksum=ROOT.checksum,
            label_crc=ROOT.label_crc,
            size=ROOT.size,
        )
        patched_loader({ROOT_KEY: [self_ref]})

        with pytest.raises(CatalogTreeError, match="already visited object"):
            discover_catalog_assets(
                object(),  # type: ignore[arg-type]
                ROOT_VERSION,
                game_version=GAME_VERSION,
                output=tmp_path,
            )

    def test_two_hop_cycle(self, tmp_path: Path, patched_loader: Any) -> None:
        """A → B → A is rejected."""
        a = subcatalog("A")
        b = subcatalog("B")
        back_to_a = make_entry(
            "back",
            type_name="catalog",
            checksum=a.checksum,
            label_crc=a.label_crc,
            size=a.size,
        )
        patched_loader(
            {
                ROOT_KEY: [a],
                manifest_of(a).real_name(): [b],
                manifest_of(b).real_name(): [back_to_a],
            }
        )

        with pytest.raises(CatalogTreeError, match="already visited object"):
            discover_catalog_assets(
                object(),  # type: ignore[arg-type]
                ROOT_VERSION,
                game_version=GAME_VERSION,
                output=tmp_path,
            )

    def test_diamond_is_rejected(self, tmp_path: Path, patched_loader: Any) -> None:
        """Two parents pointing at one child is an error, not a dedupe.

        The label guard fires before the object guard, so a shared child with
        the same label reports a duplicate label.  Both checks reject the
        shape; only the first one reached is reported.
        """
        shared = subcatalog("shared")
        left = subcatalog("left")
        right = subcatalog("right")
        patched_loader(
            {
                ROOT_KEY: [left, right],
                manifest_of(left).real_name(): [shared],
                manifest_of(right).real_name(): [shared],
                manifest_of(shared).real_name(): [asset("x")],
            }
        )

        with pytest.raises(CatalogTreeError, match="duplicate catalog label"):
            discover_catalog_assets(
                object(),  # type: ignore[arg-type]
                ROOT_VERSION,
                game_version=GAME_VERSION,
                output=tmp_path,
            )

    def test_shared_object_with_distinct_labels_is_rejected(
        self, tmp_path: Path, patched_loader: Any
    ) -> None:
        """The same catalog object under two labels hits the visited guard."""
        shared = subcatalog("shared")
        left = subcatalog("left")
        right = subcatalog("right")
        shared_alias = make_entry(
            "alias",
            type_name="catalog",
            checksum=shared.checksum,
            label_crc=shared.label_crc,
            size=shared.size,
        )
        patched_loader(
            {
                ROOT_KEY: [left, right],
                manifest_of(left).real_name(): [shared],
                manifest_of(right).real_name(): [shared_alias],
                manifest_of(shared).real_name(): [asset("x")],
            }
        )

        with pytest.raises(CatalogTreeError, match="already visited object"):
            discover_catalog_assets(
                object(),  # type: ignore[arg-type]
                ROOT_VERSION,
                game_version=GAME_VERSION,
                output=tmp_path,
            )

    def test_duplicate_label(self, tmp_path: Path, patched_loader: Any) -> None:
        """Two distinct catalog objects sharing a label are rejected."""
        twin_a = make_entry("twin", type_name="catalog", checksum=11, label_crc=11)
        twin_b = make_entry("twin", type_name="catalog", checksum=22, label_crc=22)
        parent = subcatalog("parent")
        patched_loader(
            {
                ROOT_KEY: [twin_a],
                manifest_of(twin_a).real_name(): [parent],
                manifest_of(parent).real_name(): [twin_b],
                manifest_of(twin_b).real_name(): [asset("x")],
            }
        )

        with pytest.raises(CatalogTreeError, match="duplicate catalog label"):
            discover_catalog_assets(
                object(),  # type: ignore[arg-type]
                ROOT_VERSION,
                game_version=GAME_VERSION,
                output=tmp_path,
            )

    def test_depth_limit_allows_32(self, tmp_path: Path, patched_loader: Any) -> None:
        """A chain of 31 child catalogs (max depth 31) is allowed."""
        table, deepest = build_chain(31)
        table[ROOT_KEY] = table.pop("__root_children__")
        table[ROOT_KEY].append(asset("ok"))
        patched_loader(table)

        result = discover_catalog_assets(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
        )

        # Only the root-level asset exists; the chain itself carries no assets.
        assert [item.entry.label for item in result] == ["ok"]
        assert deepest == "c31"

    def test_depth_limit_rejects_33(self, tmp_path: Path, patched_loader: Any) -> None:
        """A chain needing depth 33 fails."""
        table, _deepest = build_chain(33)
        table[ROOT_KEY] = table.pop("__root_children__")
        patched_loader(table)

        with pytest.raises(CatalogTreeError, match="exceeds 32 levels"):
            discover_catalog_assets(
                object(),  # type: ignore[arg-type]
                ROOT_VERSION,
                game_version=GAME_VERSION,
                output=tmp_path,
            )


class TestPathSafety:
    """Path escaping must be refused before anything is returned."""

    def test_subcatalog_label_escaping_output(
        self, tmp_path: Path, patched_loader: Any
    ) -> None:
        """A sub-catalog label that resolves outside the root is rejected."""
        evil = make_entry("../evil", type_name="catalog")
        patched_loader({ROOT_KEY: [evil]})

        with pytest.raises(CatalogTreeError):
            discover_catalog_assets(
                object(),  # type: ignore[arg-type]
                ROOT_VERSION,
                game_version=GAME_VERSION,
                output=tmp_path,
            )

    def test_asset_path_escaping_output(
        self, tmp_path: Path, patched_loader: Any
    ) -> None:
        """An unsafe asset label is rejected by ``catalog_entry_to_asset``."""
        patched_loader({ROOT_KEY: [asset("../escape")]})

        with pytest.raises(CatalogTreeError, match="unsafe path component"):
            discover_catalog_assets(
                object(),  # type: ignore[arg-type]
                ROOT_VERSION,
                game_version=GAME_VERSION,
                output=tmp_path,
            )


class TestErrorPropagation:
    """Errors must surface, never be swallowed."""

    def test_invalid_version_raises(self, tmp_path: Path) -> None:
        """A malformed version is rejected before any request."""
        with pytest.raises((ValueError, Exception)):
            discover_catalog_assets(
                object(),  # type: ignore[arg-type]
                "not-a-version",
                game_version=GAME_VERSION,
                output=tmp_path,
            )

    def test_catalog_load_failure_propagates(
        self, tmp_path: Path, patched_loader: Any
    ) -> None:
        """A loader failure aborts discovery and returns nothing."""
        child = subcatalog("missing")
        patched_loader({ROOT_KEY: [asset("a"), child]})

        with pytest.raises(DownloadError, match="not in fixture table"):
            discover_catalog_assets(
                object(),  # type: ignore[arg-type]
                ROOT_VERSION,
                game_version=GAME_VERSION,
                output=tmp_path,
            )

    def test_cache_flags_are_forwarded(
        self, tmp_path: Path, patched_loader: Any
    ) -> None:
        """``use_cache``/``refresh``/``writable`` reach ``load_catalog``."""
        loader = patched_loader({ROOT_KEY: [asset("a")]})

        discover_catalog_assets(
            object(),  # type: ignore[arg-type]
            ROOT_VERSION,
            game_version=GAME_VERSION,
            output=tmp_path,
            use_cache=False,
            refresh=True,
            writable=False,
        )

        assert loader.calls == [(ROOT_KEY, False, True, False)]


class TestNoPartialResults:
    """Discovery is all-or-nothing."""

    def test_failure_after_assets_returns_nothing(
        self, tmp_path: Path, patched_loader: Any
    ) -> None:
        """Assets found before a failure are not returned."""
        child = subcatalog("broken")
        patched_loader({ROOT_KEY: [asset("a"), asset("b"), child]})

        with pytest.raises(DownloadError):
            result = discover_catalog_assets(
                object(),  # type: ignore[arg-type]
                ROOT_VERSION,
                game_version=GAME_VERSION,
                output=tmp_path,
            )
            assert result == []
