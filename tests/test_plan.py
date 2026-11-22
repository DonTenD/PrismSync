"""Tests for :mod:`prism_sync.plan` and the ``--dry-run`` command."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import prism_sync.cli as cli_module
import prism_sync.plan as plan_module
from prism_sync.cli import main
from prism_sync.downloader import AssetDownloader
from prism_sync.models import (
    AssetInfoEntry,
    CatalogAsset,
    CatalogEntry,
    ResourceType,
)
from prism_sync.plan import build_sync_plan, free_bytes_for
from prism_sync.selection import Selection

VALID_VERSION = "20006500@Y/Hfn4F3ipto1cz9Xl5Djro="
PROFILE_OK = {"gameVersion": "9.9.9", "versionHash": "abc"}


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------


class _NoNetClient:
    """Stand-in HttpClient that fails if any request is attempted."""

    def __init__(self, **kwargs: object) -> None:
        pass

    def __enter__(self) -> _NoNetClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _asset(
    root: Path,
    type_name: str,
    size: int,
    *,
    name: str | None = None,
) -> CatalogAsset:
    """Build an asset whose output path lives under ``root``."""
    label = f"root/raw/{name or type_name}.{type_name}"
    path = root / "raw" / f"{name or type_name}.{type_name}"
    return CatalogAsset(
        entry=CatalogEntry(
            label=label,
            type_name=type_name,
            size=size,
            checksum=0,
            seed=0,
            label_crc=0,
            priority=0,
            resource_type=ResourceType.RawPlain,
        ),
        catalog_label="root",
        output_path=str(path),
        relative_path=f"raw/{name or type_name}.{type_name}",
        real_name=f"{name or type_name}.{type_name}",
    )


def _downloader(
    *,
    force: bool = False,
    verify: bool = False,
    previous: dict[str, dict[str, Any]] | None = None,
) -> AssetDownloader:
    return AssetDownloader(
        _NoNetClient(),  # type: ignore[arg-type]
        force=force,
        verify=verify,
        previous_entries=previous or {},
    )


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Return ``{relative path: (size, mtime_ns)}`` for every file."""
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# --------------------------------------------------------------------------
# build_sync_plan: classification
# --------------------------------------------------------------------------


class TestClassification:
    def test_all_added(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        assets = [_asset(out, "txt", 10), _asset(out, "txt", 20, name="b")]
        plan = build_sync_plan(assets, output=out, downloader=_downloader())

        assert (plan.added_count, plan.added_bytes) == (2, 30)
        assert (plan.changed_count, plan.unchanged_count) == (0, 0)
        assert plan.required_bytes == 30

    def test_all_unchanged(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        (out / "raw").mkdir(parents=True)
        asset = _asset(out, "txt", 10)
        Path(asset.output_path).write_bytes(b"0123456789")

        plan = build_sync_plan([asset], output=out, downloader=_downloader())

        assert plan.unchanged_count == 1
        assert plan.added_count == plan.changed_count == 0
        assert plan.required_bytes == 0

    def test_all_changed(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        (out / "raw").mkdir(parents=True)
        asset = _asset(out, "txt", 10)
        Path(asset.output_path).write_bytes(b"short")

        plan = build_sync_plan([asset], output=out, downloader=_downloader())

        assert plan.changed_count == 1
        assert plan.changed_bytes == 10
        assert plan.required_bytes == 10

    def test_mixed(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        (out / "raw").mkdir(parents=True)
        added = _asset(out, "txt", 5, name="a")
        changed = _asset(out, "txt", 7, name="b")
        same = _asset(out, "txt", 3, name="c")
        Path(changed.output_path).write_bytes(b"x")
        Path(same.output_path).write_bytes(b"yyy")

        plan = build_sync_plan(
            [added, changed, same], output=out, downloader=_downloader()
        )

        assert plan.total_count == 3
        assert (plan.added_count, plan.changed_count, plan.unchanged_count) == (1, 1, 1)
        assert plan.required_bytes == 5 + 7

    def test_force_marks_everything_changed(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        (out / "raw").mkdir(parents=True)
        asset = _asset(out, "txt", 3)
        Path(asset.output_path).write_bytes(b"abc")

        plan = build_sync_plan([asset], output=out, downloader=_downloader(force=True))

        assert plan.changed_count == 1
        assert plan.unchanged_count == 0

    def test_empty_catalog(self, tmp_path: Path) -> None:
        plan = build_sync_plan([], output=tmp_path / "out", downloader=_downloader())

        assert plan.total_count == plan.selected_count == 0
        assert plan.required_bytes == 0
        assert plan.by_type == []
        assert plan.enough_space is True

    def test_missing_local_file_is_added(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        asset = _asset(out, "txt", 4)
        (out / "raw").mkdir()

        plan = build_sync_plan([asset], output=out, downloader=_downloader())

        assert plan.added_count == 1

    def test_directory_in_place_of_file_is_changed(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        (out / "raw").mkdir(parents=True)
        asset = _asset(out, "txt", 4)
        Path(asset.output_path).mkdir()

        plan = build_sync_plan([asset], output=out, downloader=_downloader())

        assert plan.changed_count == 1

    def test_by_type_is_sorted_and_partial(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        assets = [_asset(out, "zzz", 1), _asset(out, "aaa", 2)]
        plan = build_sync_plan(
            assets,
            output=out,
            selection=Selection(types=frozenset({"aaa"})),
            downloader=_downloader(),
        )

        assert [stat.type_name for stat in plan.by_type] == ["aaa"]


# --------------------------------------------------------------------------
# build_sync_plan: selection boundary
# --------------------------------------------------------------------------


class TestSelectionBoundary:
    def test_unselected_remote_assets_are_not_planned(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        assets = [_asset(out, "txt", 5), _asset(out, "png", 9)]
        plan = build_sync_plan(
            assets,
            output=out,
            selection=Selection(types=frozenset({"txt"})),
            downloader=_downloader(),
        )

        assert plan.total_count == 2
        assert plan.selected_count == 1
        assert plan.added_count == 1
        assert plan.added_bytes == 5
        assert plan.required_bytes == 5

    def test_selection_never_creates_removed(self, tmp_path: Path) -> None:
        """A local file excluded by selection is still in the catalog."""
        out = tmp_path / "out"
        kept = _asset(out, "txt", 5)
        other = _asset(out, "png", 9)
        previous = {
            kept.relative_path: {"size": 5},
            other.relative_path: {"size": 9},
        }

        plan = build_sync_plan(
            [kept, other],
            output=out,
            selection=Selection(types=frozenset({"txt"})),
            previous_entries=previous,
            downloader=_downloader(),
        )

        assert plan.removed_count == 0, "selection must not import removal"

    def test_removed_counts_against_full_catalog(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        present = _asset(out, "txt", 5)
        previous = {
            present.relative_path: {"size": 5},
            "raw/gone.png": {"size": 42},
        }

        plan = build_sync_plan(
            [present],
            output=out,
            previous_entries=previous,
            downloader=_downloader(),
        )

        assert (plan.removed_count, plan.removed_bytes) == (1, 42)

    def test_removed_ignores_state_entries_without_size(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        plan = build_sync_plan(
            [],
            output=out,
            previous_entries={"raw/x": {"status": "failed"}},
            downloader=_downloader(),
        )

        assert (plan.removed_count, plan.removed_bytes) == (1, 0)

    @pytest.mark.parametrize(
        ("selection", "expected_selected"),
        [
            (None, 3),
            (Selection(types=frozenset({"txt"})), 2),
            (Selection(include=("*keep_one*",)), 1),
            (Selection(exclude=("*skip_one*",)), 2),
            (Selection(max_size=10), 2),
        ],
    )
    def test_selection_variants(
        self, tmp_path: Path, selection: Selection | None, expected_selected: int
    ) -> None:
        out = tmp_path / "out"
        assets = [
            _asset(out, "txt", 5, name="keep_one"),
            _asset(out, "txt", 5, name="skip_one"),
            _asset(out, "png", 500, name="keep_two"),
        ]

        plan = build_sync_plan(
            assets, output=out, selection=selection, downloader=_downloader()
        )

        assert plan.selected_count == expected_selected

    def test_no_selection_selects_everything(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        assets = [_asset(out, "txt", 1), _asset(out, "png", 2)]
        plan = build_sync_plan(assets, output=out, downloader=_downloader())

        assert plan.selected_count == 2
        assert plan.selection == {}


# --------------------------------------------------------------------------
# free space
# --------------------------------------------------------------------------


class TestFreeSpace:
    def test_existing_directory(self, tmp_path: Path) -> None:
        value = free_bytes_for(tmp_path)
        assert isinstance(value, int)
        assert value > 0

    def test_missing_directory_uses_the_nearest_ancestor(self, tmp_path: Path) -> None:
        deep = tmp_path / "a" / "b" / "c"
        assert free_bytes_for(deep) == free_bytes_for(tmp_path)

    def test_missing_directory_is_not_created(self, tmp_path: Path) -> None:
        deep = tmp_path / "a" / "b"
        free_bytes_for(deep)
        assert not deep.exists()

    def test_unavailable_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(path: object) -> object:
            raise OSError("no such device")

        monkeypatch.setattr(plan_module.shutil, "disk_usage", boom)
        assert free_bytes_for(tmp_path) is None

    def test_plan_reports_enough_space(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        plan = build_sync_plan(
            [_asset(out, "txt", 1)], output=out, downloader=_downloader()
        )
        assert plan.enough_space is True
        assert plan.free_bytes is not None

    def test_plan_reports_insufficient_space(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(plan_module, "free_bytes_for", lambda path: 4)
        out = tmp_path / "out"
        plan = build_sync_plan(
            [_asset(out, "txt", 10)], output=out, downloader=_downloader()
        )
        assert plan.free_bytes == 4
        assert plan.enough_space is False

    def test_plan_reports_unknown_space(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(plan_module, "free_bytes_for", lambda path: None)
        out = tmp_path / "out"
        plan = build_sync_plan(
            [_asset(out, "txt", 10)], output=out, downloader=_downloader()
        )
        assert plan.free_bytes is None
        assert plan.enough_space is None


# --------------------------------------------------------------------------
# CLI --dry-run
# --------------------------------------------------------------------------


def _patch_catalog(
    monkeypatch: pytest.MonkeyPatch,
    assets: list[CatalogAsset],
) -> dict[str, object]:
    recorded: dict[str, object] = {}

    def fake_discover(
        client: object,
        version: str,
        *,
        game_version: str,
        output: Path,
        **kwargs: object,
    ) -> tuple[list[CatalogAsset], dict[str, list[AssetInfoEntry]]]:
        recorded["discover"] = {"version": version, "output": output, **kwargs}
        return assets, {}

    def fake_resolve(profile: object, **kwargs: object) -> str:
        recorded["resolve"] = kwargs
        return VALID_VERSION

    monkeypatch.setattr(cli_module, "HttpClient", _NoNetClient)
    monkeypatch.setattr(cli_module, "discover_catalog_tree", fake_discover)
    monkeypatch.setattr(cli_module, "resolve_remote_version", fake_resolve)
    return recorded


class TestDryRunCli:
    def test_reports_plan_and_exits_zero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        out = tmp_path / "out"
        _patch_catalog(monkeypatch, [_asset(out, "txt", 10)])

        code = main(
            [
                "sync",
                "--dry-run",
                "--all",
                "--output",
                str(out),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert code == 0
        err = capsys.readouterr().err
        for field in ("added: 1", "changed: 0", "unchanged: 0", "removed: 0"):
            assert field in err
        assert "estimated download:" in err
        assert "free space:" in err

    def test_catalog_is_fetched_read_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded = _patch_catalog(monkeypatch, [])
        main(
            [
                "sync",
                "--dry-run",
                "--all",
                "--output",
                str(tmp_path / "out"),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )
        assert recorded["discover"]["writable"] is False  # type: ignore[index]

    def test_writes_nothing_at_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Real filesystem check: no directory, state or temp file appears."""
        out = tmp_path / "out"
        _patch_catalog(monkeypatch, [_asset(out, "txt", 10)])
        before = _snapshot(tmp_path)

        code = main(
            [
                "sync",
                "--dry-run",
                "--all",
                "--output",
                str(out),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert code == 0
        assert _snapshot(tmp_path) == before
        assert not out.exists(), "the output directory must not be created"

    def test_does_not_download_or_sync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_catalog(monkeypatch, [_asset(tmp_path / "out", "txt", 10)])

        def boom(*args: object, **kwargs: object) -> object:
            raise AssertionError("dry run must not download or sync")

        monkeypatch.setattr(cli_module, "sync_if_needed", boom)
        monkeypatch.setattr(cli_module, "download_assets", boom, raising=False)

        assert (
            main(
                [
                    "sync",
                    "--dry-run",
                    "--all",
                    "--output",
                    str(tmp_path / "out"),
                    "--resource-version",
                    VALID_VERSION,
                    "--game-version",
                    "2.20.0",
                ]
            )
            == 0
        )

    def test_does_not_delete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = tmp_path / "out"
        (out / "raw").mkdir(parents=True)
        stale = out / "raw" / "gone.txt"
        stale.write_bytes(b"keep me")
        _patch_catalog(monkeypatch, [_asset(out, "txt", 5)])
        state = out / "sync-state.json"
        state.write_text(
            json.dumps({"files": {"raw/gone.txt": {"size": 7}}}), encoding="utf-8"
        )

        code = main(
            [
                "sync",
                "--dry-run",
                "--all",
                "--output",
                str(out),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert code == 0
        assert stale.exists(), "removed files must never be deleted"
        assert stale.read_bytes() == b"keep me"
        assert state.exists()

    def test_removed_is_reported_from_state(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        (out / "sync-state.json").write_text(
            json.dumps({"files": {"raw/gone.png": {"size": 12}}}), encoding="utf-8"
        )
        _patch_catalog(monkeypatch, [])

        main(
            [
                "sync",
                "--dry-run",
                "--all",
                "--output",
                str(out),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert "removed: 1" in capsys.readouterr().err

    def test_state_file_is_not_rewritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        state = out / "sync-state.json"
        original = json.dumps({"files": {"raw/a.png": {"size": 3}}})
        state.write_text(original, encoding="utf-8")
        _patch_catalog(monkeypatch, [])

        main(
            [
                "sync",
                "--dry-run",
                "--all",
                "--output",
                str(out),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert state.read_text(encoding="utf-8") == original

    def test_unchanged_local_file_is_reported(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        out = tmp_path / "out"
        (out / "raw").mkdir(parents=True)
        asset = _asset(out, "txt", 3)
        Path(asset.output_path).write_bytes(b"abc")
        _patch_catalog(monkeypatch, [asset])

        main(
            [
                "sync",
                "--dry-run",
                "--all",
                "--output",
                str(out),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        err = capsys.readouterr().err
        assert "unchanged: 1" in err
        assert "added: 0" in err

    def test_selection_limits_the_plan(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        out = tmp_path / "out"
        _patch_catalog(monkeypatch, [_asset(out, "txt", 5), _asset(out, "png", 9)])

        main(
            [
                "sync",
                "--dry-run",
                "--all",
                "--output",
                str(out),
                "--types",
                "txt",
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        err = capsys.readouterr().err
        assert "selected: 1" in err
        assert "added: 1" in err

    def test_unreadable_state_warns_but_continues(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        (out / "sync-state.json").write_text("{broken", encoding="utf-8")
        _patch_catalog(monkeypatch, [])

        code = main(
            [
                "sync",
                "--dry-run",
                "--all",
                "--output",
                str(out),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert code == 0
        assert "cannot read the previous sync state" in capsys.readouterr().err

    def test_catalog_failure_exits_five(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(cli_module, "HttpClient", _NoNetClient)
        monkeypatch.setattr(
            cli_module,
            "discover_catalog_tree",
            lambda *a, **k: (_ for _ in ()).throw(OSError("catalog down")),
        )
        monkeypatch.setattr(
            cli_module, "resolve_remote_version", lambda *a, **k: VALID_VERSION
        )

        code = main(
            [
                "sync",
                "--dry-run",
                "--all",
                "--output",
                str(tmp_path / "out"),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert code == cli_module.EXIT_SYNC
        assert "cannot read the catalog" in capsys.readouterr().err

    def test_bad_explicit_version_exits_two(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(cli_module, "HttpClient", _NoNetClient)

        code = main(
            [
                "sync",
                "--dry-run",
                "--all",
                "--output",
                str(tmp_path / "out"),
                "--resource-version",
                "nope",
            ]
        )

        assert code == cli_module.EXIT_USAGE
        assert "invalid resource version" in capsys.readouterr().err


class TestDryRunConflicts:
    def test_conflicts_with_check(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(cli_module, "HttpClient", _NoNetClient)
        monkeypatch.setattr(
            cli_module, "resolve_remote_version", lambda *a, **k: VALID_VERSION
        )

        code = main(
            [
                "sync",
                "--dry-run",
                "--check",
                "--output",
                str(tmp_path / "out"),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert code == cli_module.EXIT_USAGE
        assert "cannot be combined with --check" in capsys.readouterr().err

    def test_conflicts_with_list_types(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(cli_module, "HttpClient", _NoNetClient)
        monkeypatch.setattr(
            cli_module, "resolve_remote_version", lambda *a, **k: VALID_VERSION
        )

        code = main(
            [
                "sync",
                "--dry-run",
                "--list-types",
                "--output",
                str(tmp_path / "out"),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert code == cli_module.EXIT_USAGE
        assert "cannot be combined with --list-types" in capsys.readouterr().err


class TestNormalSyncUnchanged:
    def test_dry_run_flag_does_not_leak_into_sync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without --dry-run the sync path is still used."""
        called: list[object] = []

        def fake_sync(*args: object, **kwargs: object) -> object:
            called.append(args)
            from prism_sync.sync import SyncOutcome

            return SyncOutcome(
                result=None,
                downloaded=0,
                skipped=0,
                failed=0,
                resumed=0,
                catalogs=0,
                assets=0,
                pruned=0,
                unchanged=True,
            )

        monkeypatch.setattr(cli_module, "sync_if_needed", fake_sync)
        monkeypatch.setattr(cli_module, "HttpClient", _NoNetClient)
        monkeypatch.setattr(
            cli_module, "resolve_remote_version", lambda *a, **k: VALID_VERSION
        )

        main(
            [
                "sync",
                "--all",
                "--output",
                str(tmp_path / "out"),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert len(called) == 1


# --------------------------------------------------------------------------
# removed scope: recorded state only, never a directory scan
# --------------------------------------------------------------------------


class TestRemovedScope:
    def test_unrecorded_local_file_is_not_removed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A user file never recorded in the state must not be counted."""
        out = tmp_path / "out"
        (out / "raw").mkdir(parents=True)
        (out / "raw" / "user_saved.txt").write_bytes(b"mine")
        _patch_catalog(monkeypatch, [])

        code = main(
            [
                "sync",
                "--dry-run",
                "--all",
                "--output",
                str(out),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert code == 0
        assert "removed: 0" in capsys.readouterr().err

    def test_recorded_file_that_still_exists_is_removed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Removal is a state-vs-catalog comparison, not a disk existence check."""
        out = tmp_path / "out"
        (out / "raw").mkdir(parents=True)
        kept = out / "raw" / "old.txt"
        kept.write_bytes(b"still here")
        (out / "sync-state.json").write_text(
            json.dumps({"files": {"raw/old.txt": {"size": 10}}}), encoding="utf-8"
        )
        _patch_catalog(monkeypatch, [])

        main(
            [
                "sync",
                "--dry-run",
                "--all",
                "--output",
                str(out),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert "removed: 1" in capsys.readouterr().err
        assert kept.exists(), "the dry run must not touch the file"

    def test_output_does_not_claim_a_filesystem_scan(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        (out / "sync-state.json").write_text(
            json.dumps({"files": {"raw/gone.png": {"size": 1}}}), encoding="utf-8"
        )
        _patch_catalog(monkeypatch, [])

        main(
            [
                "sync",
                "--dry-run",
                "--all",
                "--output",
                str(out),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        err = capsys.readouterr().err
        assert "previously tracked in sync state" in err
        assert "nothing is deleted" in err


class TestVerificationBasis:
    def test_recorded_size_alone_is_trusted_without_deep(self, tmp_path: Path) -> None:
        """A matching recorded size makes the file unchanged by default."""
        out = tmp_path / "out"
        (out / "raw").mkdir(parents=True)
        asset = _asset(out, "txt", 3)
        Path(asset.output_path).write_bytes(b"xyz")
        previous = {
            asset.relative_path: {
                "size": 3,
                "realName": asset.real_name,
                "sha256": "0" * 64,
            }
        }

        plan = build_sync_plan(
            [asset],
            output=out,
            previous_entries=previous,
            downloader=_downloader(previous=dict(previous)),
        )

        assert plan.unchanged_count == 1

    def test_deep_compares_sha256(self, tmp_path: Path) -> None:
        """--deep rejects a same-size file whose content hash differs."""
        out = tmp_path / "out"
        (out / "raw").mkdir(parents=True)
        asset = _asset(out, "txt", 3)
        Path(asset.output_path).write_bytes(b"xyz")
        previous = {
            asset.relative_path: {
                "size": 3,
                "realName": asset.real_name,
                "sha256": "0" * 64,
            }
        }

        plan = build_sync_plan(
            [asset],
            output=out,
            previous_entries=previous,
            downloader=AssetDownloader(
                _NoNetClient(),  # type: ignore[arg-type]
                deep=True,
                previous_entries=dict(previous),
            ),
        )

        assert plan.changed_count == 1

    def test_verify_marks_undeterminable_size_as_changed(self, tmp_path: Path) -> None:
        """LZ4/encrypted output size is unknowable, so --verify re-downloads."""
        out = tmp_path / "out"
        (out / "raw").mkdir(parents=True)
        asset = _asset(out, "bin", 100)
        object.__setattr__(asset.entry, "resource_type", ResourceType.RawLz4Compressed)
        Path(asset.output_path).write_bytes(b"x" * 50)

        plan = build_sync_plan([asset], output=out, downloader=_downloader(verify=True))

        assert plan.changed_count == 1


# --------------------------------------------------------------------------
# removed: failed / cancelled state entries
# --------------------------------------------------------------------------


class TestRemovedWithFailedEntries:
    """Failed entries count towards ``removed``; they have no byte total."""

    def test_failed_entry_is_counted_without_bytes(self) -> None:
        from prism_sync.plan import _removed_from_state

        count, total = _removed_from_state(
            {"raw/a.txt": {"size": None, "status": "failed"}}, set()
        )

        assert (count, total) == (1, 0)

    def test_skipped_entry_is_counted(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        plan = build_sync_plan(
            [],
            output=out,
            previous_entries={"raw/b.txt": {"size": 7, "status": "skipped"}},
            downloader=_downloader(),
        )

        assert (plan.removed_count, plan.removed_bytes) == (1, 7)

    def test_cancelled_entry_is_counted_if_present(self) -> None:
        """Real syncs never write ``cancelled``, but the counter tolerates it."""
        from prism_sync.plan import _removed_from_state

        count, total = _removed_from_state(
            {"raw/c.txt": {"status": "cancelled"}}, set()
        )

        assert (count, total) == (1, 0)

    def test_failed_entry_still_in_catalog_is_not_removed(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        asset = _asset(out, "txt", 5)
        plan = build_sync_plan(
            [asset],
            output=out,
            previous_entries={asset.relative_path: {"size": None, "status": "failed"}},
            downloader=_downloader(),
        )

        assert plan.removed_count == 0

    def test_failed_entry_reported_by_the_cli(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A failed download that vanished from the catalog shows as removed."""
        out = tmp_path / "out"
        out.mkdir()
        (out / "sync-state.json").write_text(
            json.dumps(
                {"files": {"raw/never.txt": {"size": None, "status": "failed"}}}
            ),
            encoding="utf-8",
        )
        _patch_catalog(monkeypatch, [])

        code = main(
            [
                "sync",
                "--dry-run",
                "--all",
                "--output",
                str(out),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert code == 0
        err = capsys.readouterr().err
        assert "removed: 1" in err
        assert not (out / "raw" / "never.txt").exists(), "no file was ever created"
