"""Tests for the version-aware synchronization wrapper."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any, ClassVar

import pytest

from prism_sync.crc import root_label_crc
from prism_sync.downloader import DownloadResult
from prism_sync.errors import DownloadCancelled
from prism_sync.models import (
    CatalogEntry,
    CatalogManifest,
    ResourceType,
    SyncOutcome,
    SyncResult,
    VersionProfile,
)
from prism_sync.selection import Selection
from prism_sync.sync import sync_if_needed
from prism_sync.varint import encode_varint
from prism_sync.version import VERSION_MARKER_NAME, read_local_version

GAME_VERSION = "2.19.1"
ROOT_SIMPLE = "if-needed-simple"


# --------------------------------------------------------------------------
# Catalog fixtures
# --------------------------------------------------------------------------


def _digest(text: str, salt: str) -> int:
    raw = hashlib.sha256(f"{salt}:{text}".encode()).digest()[:6]
    return int.from_bytes(raw, "big") or 1


def make_entry(label: str, *, size: int = 10) -> CatalogEntry:
    return CatalogEntry(
        label=label,
        type_name="asset",
        size=size,
        checksum=_digest(label, "c"),
        seed=0,
        label_crc=_digest(label, "l"),
        priority=0,
        resource_type=ResourceType.RawPlain,
        resource_type_value=int(ResourceType.RawPlain),
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


class FakeAssetDownloader:
    instances: ClassVar[list[FakeAssetDownloader]] = []

    def __init__(self, client: object, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.stop_event = kwargs.get("stop_event")
        self.downloaded: list[str] = []
        FakeAssetDownloader.instances.append(self)

    def download(self, asset: Any) -> DownloadResult:
        self.downloaded.append(asset.relative_path)
        return DownloadResult(status="downloaded", size=4, raw_size=4, verified=True)


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    entries: list[CatalogEntry] | None = None,
    behaviours: dict[str, Any] | None = None,
) -> None:
    FakeAssetDownloader.instances = []
    table = {ROOT_KEY: entries if entries is not None else [make_entry("a")]}
    monkeypatch.setattr(
        "prism_sync.catalog_client.load_catalog", FakeCatalogLoader(table)
    )
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


def _marker_exists(output: Path) -> bool:
    return (output / VERSION_MARKER_NAME).is_file()


def _write_marker(
    output: Path,
    *,
    resource_version: str = VERSION,
    game_version: str = GAME_VERSION,
    selection: dict[str, object] | None = None,
) -> None:
    from prism_sync.version import write_local_version

    write_local_version(
        output,
        game_version=game_version,
        resource_version=resource_version,
        catalog_fingerprint="f" * 64,
        selection=selection,
    )


def _call(tmp_path: Path, **kwargs: Any) -> SyncOutcome:
    return sync_if_needed(
        object(),  # type: ignore[arg-type]
        kwargs.pop("version", VERSION),
        game_version=kwargs.pop("game_version", GAME_VERSION),
        output=tmp_path,
        **kwargs,
    )


# --------------------------------------------------------------------------
# Version check and skip decisions
# --------------------------------------------------------------------------


class TestSkipDecision:
    """The wrapper must not sync when the local copy is current."""

    def test_up_to_date_skips_sync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch)
        _write_marker(tmp_path)

        outcome = _call(tmp_path)

        assert outcome.check.up_to_date is True
        assert outcome.result is None
        assert outcome.marker_written is False
        assert FakeAssetDownloader.instances == []

    def test_skip_does_not_touch_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch)
        _write_marker(tmp_path)
        before = (tmp_path / VERSION_MARKER_NAME).read_bytes()

        _call(tmp_path)

        assert (tmp_path / VERSION_MARKER_NAME).read_bytes() == before

    def test_missing_marker_triggers_sync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch)

        outcome = _call(tmp_path)

        assert outcome.check.up_to_date is False
        assert outcome.result is not None
        assert outcome.marker_written is True

    def test_different_version_triggers_sync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch)
        other = _version(
            CatalogManifest(label_crc=1, size=32, checksum=0x9999, seed=0x8888)
        )
        _write_marker(tmp_path, resource_version=other)

        outcome = _call(tmp_path)

        assert outcome.check.up_to_date is False
        assert outcome.marker_written is True

    def test_different_game_version_triggers_sync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch)
        _write_marker(tmp_path, game_version="0.0.1")

        outcome = _call(tmp_path)

        assert outcome.check.up_to_date is False
        assert outcome.marker_written is True

    @pytest.mark.parametrize("flag", ["force", "verify", "deep"])
    def test_override_flags_force_sync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
    ) -> None:
        _wire(monkeypatch)
        _write_marker(tmp_path)

        outcome = _call(tmp_path, **{flag: True})

        assert outcome.check.up_to_date is True
        assert outcome.result is not None
        assert outcome.marker_written is True
        assert FakeAssetDownloader.instances

    def test_override_flags_reach_downloader(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch)

        _call(tmp_path, force=True, verify=True, deep=True, resume=False)

        kwargs = FakeAssetDownloader.instances[-1].kwargs
        assert kwargs["force"] is True
        assert kwargs["verify"] is True
        assert kwargs["deep"] is True
        assert kwargs["resume"] is False


# --------------------------------------------------------------------------
# Sync invocation
# --------------------------------------------------------------------------


class TestSyncInvocation:
    """The wrapper must delegate with the caller's parameters."""

    def test_outcome_keeps_check_and_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch)

        outcome = _call(tmp_path)

        assert outcome.check.remote_resource_version == VERSION
        assert outcome.check.remote_simple == ROOT_SIMPLE
        assert isinstance(outcome.result, SyncResult)
        assert [a.entry.label for a in outcome.result.assets] == ["a"]

    def test_selection_is_forwarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, [make_entry("a"), make_entry("b")])

        outcome = _call(tmp_path, selection=Selection(include=("a.*",)))

        assert outcome.result is not None
        assert [a.entry.label for a in outcome.result.assets] == ["a"]

    def test_custom_state_path_is_forwarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch)
        custom = tmp_path / "custom-state.json"

        outcome = _call(tmp_path, state_path=custom)

        assert custom.is_file()
        assert outcome.result is not None
        assert outcome.result.state is not None


# --------------------------------------------------------------------------
# Marker writing conditions
# --------------------------------------------------------------------------


class TestMarkerConditions:
    """The marker is written only for a complete, clean run."""

    def test_full_success_writes_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch)

        outcome = _call(tmp_path)

        assert outcome.marker_written is True
        local = read_local_version(tmp_path)
        assert local is not None
        assert local.game_version == GAME_VERSION
        assert local.resource_version == VERSION
        assert local.catalog_fingerprint == outcome.result.catalog_fingerprint

    def test_failure_blocks_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(
            monkeypatch,
            [make_entry("a"), make_entry("b")],
            behaviours={"a": RuntimeError("boom")},
        )

        outcome = _call(tmp_path)

        assert outcome.result is not None
        assert outcome.result.failed == 1
        assert outcome.marker_written is False
        assert not _marker_exists(tmp_path)

    def test_missing_state_blocks_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch)

        def fake_sync(*args: Any, **kwargs: Any) -> SyncResult:
            return SyncResult(state=None)

        monkeypatch.setattr("prism_sync.sync.sync_resources", fake_sync)

        outcome = _call(tmp_path)

        assert outcome.marker_written is False
        assert not _marker_exists(tmp_path)

    def test_exception_in_results_blocks_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch)
        boom = ValueError("bad")

        def fake_sync(*args: Any, **kwargs: Any) -> SyncResult:
            return SyncResult(results=(boom,), state={"status": "PARTIAL"})

        monkeypatch.setattr("prism_sync.sync.sync_resources", fake_sync)

        outcome = _call(tmp_path)

        assert outcome.marker_written is False
        assert not _marker_exists(tmp_path)

    def test_download_cancelled_blocks_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch)

        def fake_sync(*args: Any, **kwargs: Any) -> SyncResult:
            return SyncResult(
                results=(DownloadCancelled("stopped"),), state={"status": "OK"}
            )

        monkeypatch.setattr("prism_sync.sync.sync_resources", fake_sync)

        outcome = _call(tmp_path)

        assert outcome.marker_written is False
        assert not _marker_exists(tmp_path)

    def test_none_result_blocks_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch)

        def fake_sync(*args: Any, **kwargs: Any) -> SyncResult:
            return SyncResult(results=(None,), state={"status": "OK"})

        monkeypatch.setattr("prism_sync.sync.sync_resources", fake_sync)

        outcome = _call(tmp_path)

        assert outcome.marker_written is False
        assert not _marker_exists(tmp_path)

    def test_marker_write_failure_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch)

        def failing_write(*args: Any, **kwargs: Any) -> None:
            raise OSError("readonly")

        monkeypatch.setattr("prism_sync.sync.write_local_version", failing_write)

        with pytest.raises(OSError, match="readonly"):
            _call(tmp_path)


# --------------------------------------------------------------------------
# marker_selection special case
# --------------------------------------------------------------------------


class TestMarkerSelection:
    """A subset sync must not narrow an existing full-sync marker."""

    def test_full_marker_survives_same_version_subset_sync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, [make_entry("a"), make_entry("b")])
        _write_marker(tmp_path, selection=None)
        subset = Selection(include=("a.*",))

        # A same-version subset sync is "up to date" and would be skipped, so
        # force it: the marker special case only runs when a sync happens.
        outcome = _call(tmp_path, selection=subset, force=True)

        assert outcome.marker_written is True
        local = read_local_version(tmp_path)
        assert local is not None
        assert local.selection is None

    def test_subset_marker_records_subset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A subset sync on a fresh install records its own selection."""
        _wire(monkeypatch, [make_entry("a"), make_entry("b")])
        subset = Selection(include=("a.*",))

        outcome = _call(tmp_path, selection=subset)

        assert outcome.marker_written is True
        local = read_local_version(tmp_path)
        assert local is not None
        assert local.selection == subset.to_dict()

    def test_subset_marker_is_kept_for_same_subset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, [make_entry("a"), make_entry("b")])
        subset = Selection(include=("a.*",))
        _write_marker(tmp_path, selection=subset.to_dict())

        outcome = _call(tmp_path, selection=subset)

        # Same version and same subset => already up to date, nothing runs.
        assert outcome.result is None
        assert outcome.marker_written is False

    def test_full_sync_records_no_selection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch)

        outcome = _call(tmp_path)

        assert outcome.marker_written is True
        local = read_local_version(tmp_path)
        assert local is not None
        assert local.selection is None

    def test_previous_full_marker_with_other_version_overwrites(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The special case needs the same version; a new version resets it."""
        _wire(monkeypatch, [make_entry("a"), make_entry("b")])
        other = _version(
            CatalogManifest(label_crc=1, size=32, checksum=0x9999, seed=0x8888)
        )
        _write_marker(tmp_path, resource_version=other, selection=None)
        subset = Selection(include=("a.*",))

        outcome = _call(tmp_path, selection=subset)

        assert outcome.marker_written is True
        local = read_local_version(tmp_path)
        assert local is not None
        assert local.resource_version == VERSION
        assert local.selection == subset.to_dict()

    def test_previous_full_marker_with_other_game_version_overwrites(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, [make_entry("a"), make_entry("b")])
        _write_marker(tmp_path, game_version="0.0.1", selection=None)
        subset = Selection(include=("a.*",))

        outcome = _call(tmp_path, selection=subset)

        assert outcome.marker_written is True
        local = read_local_version(tmp_path)
        assert local is not None
        assert local.selection == subset.to_dict()


# --------------------------------------------------------------------------
# Profile handling
# --------------------------------------------------------------------------


class TestGameVersionUsage:
    def test_game_version_drives_comparison(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch)
        _write_marker(tmp_path, game_version=GAME_VERSION)

        outcome = _call(tmp_path)

        assert outcome.check.game_version == GAME_VERSION
        assert outcome.check.up_to_date is True

    def test_profile_is_not_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch)

        outcome = _call(tmp_path)

        assert isinstance(outcome.check, object)
        assert outcome.check.remote_resource_version == VERSION

    def test_version_error_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch)

        with pytest.raises(Exception, match="invalid"):
            sync_if_needed(
                object(),  # type: ignore[arg-type]
                "not-a-version",
                game_version=GAME_VERSION,
                output=tmp_path,
            )

    def test_custom_profile_hash_is_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``VersionProfile`` still round-trips through ``check_version``."""
        from prism_sync.version import check_version

        _write_marker(tmp_path)
        profile = VersionProfile(game_version=GAME_VERSION, version_hash="abc")

        check = check_version(profile, tmp_path, VERSION)

        assert check.up_to_date is True
        assert check.game_version == GAME_VERSION
