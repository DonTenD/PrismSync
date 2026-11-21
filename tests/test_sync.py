"""Tests for the minimal synchronization orchestration."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from pathlib import Path
from typing import Any, ClassVar

import pytest

from prism_sync.catalog_client import catalog_fingerprint
from prism_sync.crc import root_label_crc
from prism_sync.downloader import DownloadResult
from prism_sync.errors import CatalogTreeError, DownloadCancelled
from prism_sync.models import (
    AssetInfoEntry,
    CatalogEntry,
    CatalogManifest,
    ResourceType,
    SyncResult,
)
from prism_sync.selection import Selection
from prism_sync.state import SyncStateStore
from prism_sync.sync import sync_resources
from prism_sync.varint import encode_varint

GAME_VERSION = "1.0"
ROOT_SIMPLE = "sync-simple"


# --------------------------------------------------------------------------
# Catalog fixtures
# --------------------------------------------------------------------------


def _digest(text: str, salt: str) -> int:
    raw = hashlib.sha256(f"{salt}:{text}".encode()).digest()[:6]
    return int.from_bytes(raw, "big") or 1


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


ROOT = CatalogManifest(
    label_crc=root_label_crc(GAME_VERSION, ROOT_SIMPLE),
    size=64,
    checksum=0x1111,
    seed=0x2222,
)
ROOT_KEY = ROOT.real_name()


def _version(manifest: CatalogManifest) -> str:
    payload = (
        manifest.checksum.to_bytes(8, "big")
        + encode_varint(manifest.size)
        + manifest.seed.to_bytes(8, "big")
    )
    return f"{ROOT_SIMPLE}@{base64.b64encode(payload).decode('ascii')}"


VERSION = _version(ROOT)


class FakeCatalogLoader:
    def __init__(self, table: dict[str, list[CatalogEntry]]) -> None:
        self.table = table

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
        return list(self.table[manifest.real_name()])


# --------------------------------------------------------------------------
# Downloader fixtures
# --------------------------------------------------------------------------


class FakeAssetDownloader:
    """Stands in for ``AssetDownloader`` inside ``sync_resources``."""

    instances: ClassVar[list[FakeAssetDownloader]] = []

    def __init__(self, client: object, **kwargs: Any) -> None:
        self.client = client
        self.kwargs = kwargs
        self.previous_entries = kwargs.get("previous_entries")
        self.stop_event = kwargs.get("stop_event")
        self.downloaded: list[str] = []
        FakeAssetDownloader.instances.append(self)

    def download(self, asset: Any) -> DownloadResult:
        self.downloaded.append(asset.relative_path)
        return DownloadResult(status="downloaded", size=4, raw_size=4, verified=True)


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    table: dict[str, list[CatalogEntry]],
    behaviours: dict[str, Any] | None = None,
) -> type[FakeAssetDownloader]:
    """Patch catalog loading and asset downloading for one test."""
    FakeAssetDownloader.instances = []
    loader = FakeCatalogLoader(table)
    monkeypatch.setattr("prism_sync.catalog_client.load_catalog", loader)
    monkeypatch.setattr("prism_sync.sync.AssetDownloader", FakeAssetDownloader)

    if behaviours:

        def download(self: FakeAssetDownloader, asset: Any) -> DownloadResult:
            self.downloaded.append(asset.relative_path)
            behaviour = behaviours.get(asset.entry.label)
            if callable(behaviour):
                return behaviour(asset)
            if isinstance(behaviour, BaseException):
                raise behaviour
            if behaviour is not None:
                return behaviour
            return DownloadResult(
                status="downloaded", size=4, raw_size=4, verified=True
            )

        monkeypatch.setattr(FakeAssetDownloader, "download", download)
    return FakeAssetDownloader


def _sync(
    tmp_path: Path,
    *,
    selection: Selection | None = None,
    stop_event: threading.Event | None = None,
    **kwargs: Any,
) -> SyncResult:
    return sync_resources(
        object(),  # type: ignore[arg-type]
        VERSION,
        game_version=GAME_VERSION,
        output=tmp_path,
        selection=selection,
        stop_event=stop_event,
        **kwargs,
    )


def _state(tmp_path: Path) -> dict[str, Any]:
    return SyncStateStore(tmp_path / "sync-state.json").load()


# --------------------------------------------------------------------------
# Basic behaviour
# --------------------------------------------------------------------------


class TestFirstSync:
    def test_downloads_everything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, {ROOT_KEY: [make_entry("a"), make_entry("b")]})

        result = _sync(tmp_path)

        assert result.downloaded == 2
        assert result.skipped == 0
        assert result.failed == 0
        assert [a.entry.label for a in result.assets] == ["a", "b"]
        assert all(isinstance(item, DownloadResult) for item in result.results)

    def test_results_align_with_assets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, {ROOT_KEY: [make_entry("a"), make_entry("b")]})

        result = _sync(tmp_path)

        assert len(result.results) == len(result.assets)

    def test_selection_filters_assets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, {ROOT_KEY: [make_entry("a"), make_entry("b")]})

        result = _sync(tmp_path, selection=Selection(include=("a.*",)))

        assert [a.entry.label for a in result.assets] == ["a"]
        assert result.total_asset_count == 2
        assert result.downloaded == 1

    def test_previous_entries_are_forwarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, {ROOT_KEY: [make_entry("a")]})
        store = SyncStateStore(tmp_path / "sync-state.json")
        store.save({"files": {"root/a.asset": {"realName": "x", "size": 4}}})

        _sync(tmp_path)

        assert FakeAssetDownloader.instances[-1].previous_entries == {
            "root/a.asset": {"realName": "x", "size": 4}
        }

    def test_options_reach_the_downloader(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, {ROOT_KEY: [make_entry("a")]})

        _sync(tmp_path, force=True, verify=True, deep=True, resume=False)

        kwargs = FakeAssetDownloader.instances[-1].kwargs
        assert kwargs["force"] is True
        assert kwargs["verify"] is True
        assert kwargs["deep"] is True
        assert kwargs["resume"] is False


class TestSkipAndForce:
    def test_skipped_assets_are_counted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(
            monkeypatch,
            {ROOT_KEY: [make_entry("a"), make_entry("b")]},
            behaviours={
                "a": DownloadResult(
                    status="skipped", size=4, raw_size=4, verified=False
                )
            },
        )

        result = _sync(tmp_path)

        assert result.skipped == 1
        assert result.downloaded == 1
        assert result.failed == 0

    def test_resumed_is_counted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(
            monkeypatch,
            {ROOT_KEY: [make_entry("a")]},
            behaviours={
                "a": DownloadResult(
                    status="downloaded",
                    size=4,
                    raw_size=4,
                    verified=True,
                    resumed=True,
                )
            },
        )

        result = _sync(tmp_path)

        assert result.resumed == 1
        assert result.downloaded == 1


# --------------------------------------------------------------------------
# State snapshot
# --------------------------------------------------------------------------


class TestStateSnapshot:
    def test_ok_status_and_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, {ROOT_KEY: [make_entry("a")]})

        result = _sync(tmp_path)
        state = _state(tmp_path)

        assert state["status"] == "OK"
        assert state["gameVersion"] == GAME_VERSION
        assert state["resourceVersion"] == VERSION
        assert state["catalogFingerprint"] == result.catalog_fingerprint
        assert set(state) == {
            "checkedAt",
            "status",
            "gameVersion",
            "resourceVersion",
            "catalogFingerprint",
            "summary",
            "selection",
            "files",
        }
        assert set(state["summary"]) == {
            "catalogs",
            "assets",
            "totalAssets",
            "downloaded",
            "skipped",
            "failed",
            "resumed",
            "pruned",
            "localizetextCategories",
        }
        assert state["summary"]["downloaded"] == 1
        assert state["summary"]["pruned"] == 0
        assert state["summary"]["localizetextCategories"] is None
        assert state["selection"] is None
        assert result.state == state

    def test_file_record_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, {ROOT_KEY: [make_entry("a")]})

        _sync(tmp_path)
        record = _state(tmp_path)["files"]["root/a.asset"]

        assert set(record) == {
            "size",
            "rawSize",
            "realName",
            "status",
            "verified",
            "rawMd5",
            "sha256",
            "etag",
        }
        assert record["status"] == "downloaded"
        assert record["realName"] == make_entry("a").as_manifest().real_name()

    def test_failed_record_has_error_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(
            monkeypatch,
            {ROOT_KEY: [make_entry("a")]},
            behaviours={"a": RuntimeError("boom")},
        )

        _sync(tmp_path)
        record = _state(tmp_path)["files"]["root/a.asset"]

        assert record["status"] == "failed"
        assert record["size"] is None
        assert record["error"] == "boom"

    def test_partial_status_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(
            monkeypatch,
            {ROOT_KEY: [make_entry("a"), make_entry("b")]},
            behaviours={"a": RuntimeError("boom")},
        )

        result = _sync(tmp_path)

        assert result.failed == 1
        assert result.downloaded == 1
        assert _state(tmp_path)["status"] == "PARTIAL"

    def test_fingerprint_uses_full_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        child = make_entry("sub", type_name="catalog")
        _wire(
            monkeypatch,
            {
                ROOT_KEY: [make_entry("a"), child],
                child.as_manifest().real_name(): [make_entry("x")],
            },
        )

        result = _sync(tmp_path)

        expected = catalog_fingerprint(
            {
                "root": [
                    AssetInfoEntry(
                        "a.asset",
                        make_entry("a").as_manifest().real_name(),
                        "RawPlain",
                    ),
                    AssetInfoEntry(
                        "sub.catalog", child.as_manifest().real_name(), "Unknown(0)"
                    ),
                ],
                "sub": [
                    AssetInfoEntry(
                        "x.asset",
                        make_entry("x").as_manifest().real_name(),
                        "RawPlain",
                    )
                ],
            }
        )
        assert result.catalog_fingerprint == expected
        assert result.catalog_count == 2

    def test_selection_is_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, {ROOT_KEY: [make_entry("a")]})

        _sync(tmp_path, selection=Selection(include=("a.*",)))

        assert _state(tmp_path)["selection"] == Selection(include=("a.*",)).to_dict()

    def test_custom_state_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, {ROOT_KEY: [make_entry("a")]})
        custom = tmp_path / "nested" / "custom-state.json"

        _sync(tmp_path, state_path=custom)

        assert custom.is_file()
        assert not (tmp_path / "sync-state.json").exists()

    def test_state_write_failure_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, {ROOT_KEY: [make_entry("a")]})

        def failing_save(self: Any, state: dict[str, Any]) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(SyncStateStore, "save", failing_save)

        with pytest.raises(OSError, match="disk full"):
            _sync(tmp_path)


class TestPreviousState:
    def test_second_sync_reuses_previous_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, {ROOT_KEY: [make_entry("a")]})
        _sync(tmp_path)
        first = _state(tmp_path)

        _sync(tmp_path)

        assert FakeAssetDownloader.instances[-1].previous_entries == first["files"]

    def test_dropped_files_are_removed_from_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Old records for paths absent from the catalog are not re-kept."""
        _wire(monkeypatch, {ROOT_KEY: [make_entry("a")]})
        SyncStateStore(tmp_path / "sync-state.json").save(
            {
                "files": {
                    "root/gone.asset": {"size": 4, "status": "downloaded"},
                    "root/a.asset": {"size": 4, "status": "downloaded"},
                }
            }
        )

        _sync(tmp_path)

        files = _state(tmp_path)["files"]
        assert "root/gone.asset" not in files
        assert "root/a.asset" in files

    def test_corrupt_state_falls_back_to_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, {ROOT_KEY: [make_entry("a")]})
        (tmp_path / "sync-state.json").write_text("not json", encoding="utf-8")

        result = _sync(tmp_path)

        assert result.downloaded == 1
        assert FakeAssetDownloader.instances[-1].previous_entries == {}

    def test_non_dict_files_falls_back_to_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, {ROOT_KEY: [make_entry("a")]})
        SyncStateStore(tmp_path / "sync-state.json").save({"files": ["nope"]})

        _sync(tmp_path)

        assert FakeAssetDownloader.instances[-1].previous_entries == {}


# --------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------


class TestCancellation:
    def test_preset_stop_event_writes_no_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An event set before the batch starts cancels everything."""
        _wire(monkeypatch, {ROOT_KEY: [make_entry("a"), make_entry("b")]})
        event = threading.Event()
        event.set()

        result = _sync(tmp_path, stop_event=event)

        assert result.downloaded == 0
        assert result.failed == 0
        assert result.state is None
        assert not (tmp_path / "sync-state.json").exists()

    def test_midbatch_cancel_keeps_completed_results(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        event = threading.Event()

        behaviour: dict[str, Any] = {}

        def cancel_after_first(asset: Any) -> DownloadResult:
            event.set()
            return DownloadResult(
                status="downloaded", size=4, raw_size=4, verified=True
            )

        behaviour["a"] = cancel_after_first
        _wire(
            monkeypatch,
            {ROOT_KEY: [make_entry("a"), make_entry("b")]},
            behaviours=behaviour,
        )

        result = _sync(tmp_path, stop_event=event, jobs=1)

        assert isinstance(result.results[0], DownloadResult)
        assert result.results[1] is None
        assert result.state is None
        assert not (tmp_path / "sync-state.json").exists()

    def test_download_cancelled_is_not_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        event = threading.Event()

        def cancel(asset: Any) -> DownloadResult:
            event.set()
            raise DownloadCancelled("stopped")

        _wire(
            monkeypatch,
            {ROOT_KEY: [make_entry("a"), make_entry("b")]},
            behaviours={"a": cancel},
        )

        result = _sync(tmp_path, stop_event=event, jobs=1)

        assert result.failed == 0
        assert isinstance(result.results[0], DownloadCancelled)
        assert result.state is None

    def test_none_is_not_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        event = threading.Event()

        def stop_after(asset: Any) -> DownloadResult:
            event.set()
            return DownloadResult(
                status="downloaded", size=4, raw_size=4, verified=True
            )

        _wire(
            monkeypatch,
            {ROOT_KEY: [make_entry("a"), make_entry("b"), make_entry("c")]},
            behaviours={"a": stop_after},
        )

        result = _sync(tmp_path, stop_event=event, jobs=1)

        assert result.failed == 0
        assert result.results.count(None) >= 1
        assert result.state is None


# --------------------------------------------------------------------------
# Propagated exceptions
# --------------------------------------------------------------------------


class TestPropagatedExceptions:
    def test_catalog_failure_writes_no_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, {})  # ROOT_KEY missing -> KeyError

        with pytest.raises(KeyError):
            _sync(tmp_path)

        assert not (tmp_path / "sync-state.json").exists()

    def test_keyboard_interrupt_propagates_without_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(
            monkeypatch,
            {ROOT_KEY: [make_entry("a")]},
            behaviours={"a": KeyboardInterrupt()},
        )

        with pytest.raises(KeyboardInterrupt):
            _sync(tmp_path)

        assert not (tmp_path / "sync-state.json").exists()

    def test_progress_callback_failure_propagates_without_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, {ROOT_KEY: [make_entry("a")]})

        def explode(asset: Any, done: int, total: int) -> None:
            raise RuntimeError("callback boom")

        with pytest.raises(RuntimeError, match="callback boom"):
            _sync(tmp_path, on_progress=explode)

        assert not (tmp_path / "sync-state.json").exists()

    def test_tree_error_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        evil = make_entry("../evil", type_name="catalog")
        _wire(monkeypatch, {ROOT_KEY: [evil]})

        with pytest.raises(CatalogTreeError):
            _sync(tmp_path)


# --------------------------------------------------------------------------
# Progress callback forwarding
# --------------------------------------------------------------------------


class TestProgressForwarding:
    def test_callback_receives_every_asset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, {ROOT_KEY: [make_entry("a"), make_entry("b")]})
        events: list[tuple[str, int, int]] = []

        _sync(
            tmp_path,
            on_progress=lambda a, d, t: events.append((a.relative_path, d, t)),
            jobs=1,
        )

        assert [done for _, done, _ in events] == [1, 2]
        assert all(total == 2 for _, _, total in events)


# --------------------------------------------------------------------------
# Snapshot JSON validity
# --------------------------------------------------------------------------


class TestSnapshotSerializable:
    def test_snapshot_has_no_exception_objects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Failures are stored as text so the snapshot stays JSON-serializable."""
        _wire(
            monkeypatch,
            {ROOT_KEY: [make_entry("a"), make_entry("b")]},
            behaviours={"a": ValueError("bad")},
        )

        _sync(tmp_path)
        raw = (tmp_path / "sync-state.json").read_text(encoding="utf-8")

        assert json.loads(raw)  # parses
        assert "bad" in raw
        assert "ValueError" not in raw
