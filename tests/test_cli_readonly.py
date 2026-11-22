"""Tests for the read-only ``--check`` and ``--list-types`` commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import prism_sync.cli as cli_module
from prism_sync.catalog_stats import _build_type_stats
from prism_sync.cli import main
from prism_sync.models import (
    AssetInfoEntry,
    CatalogAsset,
    CatalogEntry,
    LocalVersion,
    VersionCheck,
    VersionProfile,
)
from prism_sync.selection import Selection, TypeStat
from prism_sync.version import VersionError

VALID_VERSION = "20006500@Y/Hfn4F3ipto1cz9Xl5Djro="
PROFILE_OK = {"gameVersion": "9.9.9", "versionHash": "abc"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _asset(type_name: str, size: int) -> CatalogAsset:
    label = f"root/raw/x.{type_name}"
    return CatalogAsset(
        entry=CatalogEntry(
            label=label,
            type_name=type_name,
            size=size,
            checksum=0,
            seed=0,
            label_crc=0,
            priority=0,
        ),
        catalog_label="root",
        output_path="",
        relative_path=label,
        real_name=f"x.{type_name}",
    )


def _write(tmp_path: Path, name: str, payload: object) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _patch_remote(
    monkeypatch: pytest.MonkeyPatch, *, version: str = VALID_VERSION
) -> dict[str, object]:
    """Replace the remote version lookup and record the call."""
    recorded: dict[str, object] = {"calls": []}

    def fake_resolve(profile: VersionProfile, **kwargs: object) -> str:
        recorded["calls"].append({"profile": profile, **kwargs})
        recorded["resolve_kwargs"] = kwargs
        return version

    def boom(*args: object, **kwargs: object) -> str:
        raise AssertionError("a real gRPC lookup must not happen")

    monkeypatch.setattr(cli_module, "resolve_remote_version", fake_resolve)
    monkeypatch.setattr(cli_module, "fetch_resource_version", boom, raising=False)
    return recorded


def _no_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if either command reaches download or sync code."""

    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("must not run during a read-only command")

    monkeypatch.setattr(cli_module, "sync_if_needed", boom)
    monkeypatch.setattr(cli_module, "HttpClient", boom)


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Return ``{relative path: (size, mtime_ns)}`` for ``root``."""
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _fake_catalog(
    monkeypatch: pytest.MonkeyPatch,
    assets: list[CatalogAsset],
) -> dict[str, object]:
    """Replace catalog discovery and record how it was invoked."""
    recorded: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            recorded["client_kwargs"] = kwargs

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def fake_discover(
        client: object,
        version: str,
        *,
        game_version: str,
        output: Path,
        **kwargs: object,
    ) -> tuple[list[CatalogAsset], dict[str, list[AssetInfoEntry]]]:
        recorded["discover"] = {
            "version": version,
            "game_version": game_version,
            "output": output,
            **kwargs,
        }
        return assets, {}

    monkeypatch.setattr(cli_module, "HttpClient", _FakeClient)
    monkeypatch.setattr(cli_module, "discover_catalog_tree", fake_discover)
    return recorded


# --------------------------------------------------------------------------
# --check
# --------------------------------------------------------------------------


class TestCheck:
    def test_up_to_date(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        monkeypatch.setattr(
            cli_module,
            "check_version",
            lambda *a, **k: VersionCheck(
                game_version="2.20.0",
                remote_resource_version=VALID_VERSION,
                remote_simple="20006500",
                local=LocalVersion(
                    game_version="2.20.0",
                    resource_version=VALID_VERSION,
                    catalog_fingerprint="fp",
                    synced_at="2026-01-01T00:00:00Z",
                    selection=None,
                ),
                up_to_date=True,
                reason=None,
            ),
        )
        _patch_remote(monkeypatch)
        _no_execution(monkeypatch)

        code = main(
            [
                "sync",
                "--check",
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
        assert "remote resource version: " in err
        assert "status: up-to-date" in err

    def test_update_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        monkeypatch.setattr(
            cli_module,
            "check_version",
            lambda *a, **k: VersionCheck(
                game_version="2.20.0",
                remote_resource_version=VALID_VERSION,
                remote_simple="20006500",
                local=LocalVersion(
                    game_version="2.20.0",
                    resource_version="19999999@old=",
                    catalog_fingerprint="fp",
                    synced_at="2026-01-01T00:00:00Z",
                    selection=None,
                ),
                up_to_date=False,
                reason="resource version differs",
            ),
        )
        _patch_remote(monkeypatch)
        _no_execution(monkeypatch)

        code = main(
            [
                "sync",
                "--check",
                "--output",
                str(out),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert code == 1
        err = capsys.readouterr().err
        assert "local resource version: 19999999@old=" in err
        assert "status: update-available" in err

    def test_missing_local_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        monkeypatch.setattr(
            cli_module,
            "check_version",
            lambda *a, **k: VersionCheck(
                game_version="2.20.0",
                remote_resource_version=VALID_VERSION,
                remote_simple="20006500",
                local=None,
                up_to_date=False,
                reason="local version marker not found",
            ),
        )
        _patch_remote(monkeypatch)
        _no_execution(monkeypatch)

        code = main(
            [
                "sync",
                "--check",
                "--output",
                str(out),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert code == 1
        err = capsys.readouterr().err
        assert "local resource version: (none)" in err
        assert "no local version marker" in err

    def test_remote_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        def boom(*args: object, **kwargs: object) -> str:
            raise VersionError("grpc unreachable")

        monkeypatch.setattr(cli_module, "resolve_remote_version", boom)
        _no_execution(monkeypatch)

        code = main(
            [
                "sync",
                "--check",
                "--output",
                str(tmp_path / "out"),
                "--game-version",
                "2.20.0",
                "--profile",
                str(_write(tmp_path, "p.json", PROFILE_OK)),
            ]
        )

        assert code == cli_module.EXIT_VERSION
        assert "cannot resolve the remote version" in capsys.readouterr().err

    def test_invalid_resource_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        _no_execution(monkeypatch)

        code = main(
            [
                "sync",
                "--check",
                "--output",
                str(tmp_path / "out"),
                "--resource-version",
                "nope",
            ]
        )

        assert code == cli_module.EXIT_USAGE
        assert "invalid resource version" in capsys.readouterr().err

    def test_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = tmp_path / "out"
        before = _snapshot(tmp_path)
        monkeypatch.setattr(
            cli_module,
            "check_version",
            lambda *a, **k: VersionCheck(
                game_version="2.20.0",
                remote_resource_version=VALID_VERSION,
                remote_simple="20006500",
                local=None,
                up_to_date=False,
                reason="missing",
            ),
        )
        _patch_remote(monkeypatch)
        _no_execution(monkeypatch)

        main(
            [
                "sync",
                "--check",
                "--output",
                str(out),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert _snapshot(tmp_path) == before
        assert not out.exists(), "the output directory must not be created"

    def test_does_not_require_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A read-only command must not be blocked by the --all guard."""
        monkeypatch.setattr(
            cli_module,
            "check_version",
            lambda *a, **k: VersionCheck(
                game_version="2.20.0",
                remote_resource_version=VALID_VERSION,
                remote_simple="20006500",
                local=None,
                up_to_date=False,
                reason="missing",
            ),
        )
        _patch_remote(monkeypatch)
        _no_execution(monkeypatch)

        code = main(
            [
                "sync",
                "--check",
                "--output",
                str(tmp_path / "out"),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert code == 1

    @pytest.mark.parametrize(
        "extra",
        [
            ["--types", "txt"],
            ["--include", "*.txt"],
            ["--exclude", "*.png"],
            ["--max-size", "1KB"],
        ],
    )
    def test_selection_is_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        extra: list[str],
    ) -> None:
        """A global version check cannot honour resource filters."""
        _patch_remote(monkeypatch)
        _no_execution(monkeypatch)

        code = main(
            [
                "sync",
                "--check",
                "--output",
                str(tmp_path / "out"),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
                *extra,
            ]
        )

        assert code == cli_module.EXIT_USAGE
        assert "does not support resource filters" in capsys.readouterr().err


# --------------------------------------------------------------------------
# --list-types
# --------------------------------------------------------------------------


class TestListTypes:
    def test_counts_and_totals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        assets = [_asset("txt", 10), _asset("txt", 20), _asset("png", 5)]
        recorded = _fake_catalog(monkeypatch, assets)
        _patch_remote(monkeypatch)

        code = main(
            [
                "sync",
                "--list-types",
                "--output",
                str(tmp_path / "out"),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert code == 0
        err = capsys.readouterr().err
        assert "png" in err and "txt" in err
        assert "total" in err
        assert "35" in err, "10 + 20 + 5 must appear as the byte total"
        assert recorded["discover"]["version"] == VALID_VERSION
        assert recorded["discover"]["writable"] is False

    def test_sorted_by_type_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        assets = [_asset("zzz", 1), _asset("aaa", 1), _asset("mmm", 1)]
        _fake_catalog(monkeypatch, assets)
        _patch_remote(monkeypatch)

        main(
            [
                "sync",
                "--list-types",
                "--output",
                str(tmp_path / "out"),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        lines = [
            line
            for line in capsys.readouterr().err.splitlines()
            if "type" not in line
            and "total" not in line
            and "remote resource" not in line
        ]
        names = [line.split()[2] for line in lines]
        assert names == ["aaa", "mmm", "zzz"]

    def test_empty_catalog(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        _fake_catalog(monkeypatch, [])
        _patch_remote(monkeypatch)

        code = main(
            [
                "sync",
                "--list-types",
                "--output",
                str(tmp_path / "out"),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert code == 0
        err = capsys.readouterr().err
        assert "total" in err
        assert "0" in err

    def test_selection_filters_selected_counts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        assets = [_asset("txt", 10), _asset("png", 5)]
        _fake_catalog(monkeypatch, assets)
        _patch_remote(monkeypatch)

        code = main(
            [
                "sync",
                "--list-types",
                "--output",
                str(tmp_path / "out"),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
                "--types",
                "txt",
            ]
        )

        assert code == 0
        err = capsys.readouterr().err
        txt_line = next(line for line in err.splitlines() if " txt " in line)
        png_line = next(line for line in err.splitlines() if " png " in line)
        assert txt_line.split()[-1] == "1", "txt is selected"
        assert png_line.split()[-1] == "0", "png is not selected"

    def test_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_catalog(monkeypatch, [_asset("txt", 1)])
        _patch_remote(monkeypatch)
        before = _snapshot(tmp_path)

        main(
            [
                "sync",
                "--list-types",
                "--output",
                str(tmp_path / "out"),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert _snapshot(tmp_path) == before

    def test_catalog_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        class _FakeClient:
            def __init__(self, **kwargs: object) -> None: ...

            def __enter__(self) -> _FakeClient:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        def boom(*args: object, **kwargs: object) -> object:
            raise OSError("catalog unavailable")

        monkeypatch.setattr(cli_module, "HttpClient", _FakeClient)
        monkeypatch.setattr(cli_module, "discover_catalog_tree", boom)
        _patch_remote(monkeypatch)

        code = main(
            [
                "sync",
                "--list-types",
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


# --------------------------------------------------------------------------
# parameter combinations
# --------------------------------------------------------------------------


class TestCombinations:
    def test_check_and_list_types_conflict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        _no_execution(monkeypatch)
        _patch_remote(monkeypatch)

        code = main(
            [
                "sync",
                "--check",
                "--list-types",
                "--output",
                str(tmp_path / "o"),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert code == cli_module.EXIT_USAGE
        assert "cannot be combined" in capsys.readouterr().err

    def test_read_only_commands_skip_download(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_catalog(monkeypatch, [_asset("txt", 1)])
        _patch_remote(monkeypatch)

        def boom(*args: object, **kwargs: object) -> object:
            raise AssertionError("downloads must not start")

        monkeypatch.setattr(cli_module, "sync_if_needed", boom)
        monkeypatch.setattr(cli_module, "download_assets", boom, raising=False)

        assert (
            main(
                [
                    "sync",
                    "--list-types",
                    "--output",
                    str(tmp_path / "o"),
                    "--resource-version",
                    VALID_VERSION,
                    "--game-version",
                    "2.20.0",
                ]
            )
            == 0
        )


# --------------------------------------------------------------------------
# build_type_stats
# --------------------------------------------------------------------------


class TestBuildTypeStats:
    def test_splits_selected_and_total(self) -> None:
        assets = [_asset("txt", 10), _asset("png", 5)]
        stats = _build_type_stats(assets, Selection(types=frozenset({"txt"})))

        assert stats == [
            TypeStat("png", 1, 5, 0, 0),
            TypeStat("txt", 1, 10, 1, 10),
        ]

    def test_empty_input(self) -> None:
        assert _build_type_stats([], Selection()) == []

    def test_sorted_output(self) -> None:
        assets = [_asset("b", 1), _asset("a", 1), _asset("c", 1)]
        names = [stat.type_name for stat in _build_type_stats(assets, Selection())]
        assert names == ["a", "b", "c"]

    def test_max_size_filter(self) -> None:
        assets = [_asset("txt", 10), _asset("txt", 1000)]
        stats = _build_type_stats(assets, Selection(max_size=100))
        assert stats == [TypeStat("txt", 2, 1010, 1, 10)]


# --------------------------------------------------------------------------
# error classification and read-only flag conflicts
# --------------------------------------------------------------------------


class TestErrorClassification:
    """Input errors exit 2, gRPC failures exit 4, profiles exit 3."""

    @pytest.mark.parametrize("command", ["--check", "--list-types"])
    def test_invalid_resource_version_is_a_usage_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        _no_execution(monkeypatch)

        code = main(
            [
                "sync",
                command,
                "--output",
                str(tmp_path / "out"),
                "--resource-version",
                "not-a-version",
            ]
        )

        assert code == cli_module.EXIT_USAGE
        assert "invalid resource version" in capsys.readouterr().err

    @pytest.mark.parametrize("command", ["--check", "--list-types"])
    def test_grpc_failure_is_a_version_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        def boom(*args: object, **kwargs: object) -> str:
            raise VersionError("grpc unreachable")

        monkeypatch.setattr(cli_module, "resolve_remote_version", boom)
        _no_execution(monkeypatch)

        code = main(
            [
                "sync",
                command,
                "--output",
                str(tmp_path / "out"),
                "--profile",
                str(_write(tmp_path, "p.json", PROFILE_OK)),
            ]
        )

        assert code == cli_module.EXIT_VERSION
        assert "cannot resolve the remote version" in capsys.readouterr().err

    @pytest.mark.parametrize("command", ["--check", "--list-types"])
    def test_bad_profile_still_exits_three(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        _no_execution(monkeypatch)

        code = main(
            [
                "sync",
                command,
                "--output",
                str(tmp_path / "out"),
                "--profile",
                str(tmp_path / "missing.json"),
            ]
        )

        assert code == cli_module.EXIT_PROFILE
        assert "profile file not found" in capsys.readouterr().err

    def test_decode_validation_happens_before_the_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A malformed version must not reach resolve_remote_version."""
        calls: list[object] = []

        def spy(*args: object, **kwargs: object) -> str:
            calls.append(args)
            return VALID_VERSION

        monkeypatch.setattr(cli_module, "resolve_remote_version", spy)
        _no_execution(monkeypatch)

        main(
            [
                "sync",
                "--check",
                "--output",
                str(tmp_path / "out"),
                "--resource-version",
                "no-at-sign",
            ]
        )

        assert calls == []


class TestReadOnlyFlagConflicts:
    @pytest.mark.parametrize("command", ["--check", "--list-types"])
    def test_force_is_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        _patch_remote(monkeypatch)
        _fake_catalog(monkeypatch, [])
        _no_execution(monkeypatch) if command == "--check" else None

        code = main(
            [
                "sync",
                command,
                "--output",
                str(tmp_path / "out"),
                "--force",
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert code == cli_module.EXIT_USAGE
        assert "--force is not supported" in capsys.readouterr().err

    @pytest.mark.parametrize("command", ["--check", "--list-types"])
    def test_explicit_jobs_is_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        _patch_remote(monkeypatch)

        code = main(
            [
                "sync",
                command,
                "--output",
                str(tmp_path / "out"),
                "--jobs",
                "16",
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert code == cli_module.EXIT_USAGE
        assert "--jobs is not supported" in capsys.readouterr().err

    def test_default_jobs_does_not_trigger_the_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--jobs defaults to None, so omitting it must keep working."""
        _fake_catalog(monkeypatch, [_asset("txt", 1)])
        _patch_remote(monkeypatch)

        code = main(
            [
                "sync",
                "--list-types",
                "--output",
                str(tmp_path / "out"),
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert code == 0

    def test_normal_sync_still_accepts_force_and_jobs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard must not leak into the download path."""
        seen: dict[str, object] = {}

        def fake_sync(*args: object, **kwargs: object) -> object:
            seen.update(kwargs)
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
        _patch_remote(monkeypatch)

        class _FakeClient:
            def __init__(self, **kwargs: object) -> None: ...

            def __enter__(self) -> _FakeClient:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(cli_module, "HttpClient", _FakeClient)

        main(
            [
                "sync",
                "--all",
                "--output",
                str(tmp_path / "out"),
                "--force",
                "--jobs",
                "4",
                "--resource-version",
                VALID_VERSION,
                "--game-version",
                "2.20.0",
            ]
        )

        assert seen.get("jobs") == 4
        assert seen.get("force") is True


class TestStatsArePrivate:
    def test_not_exported(self) -> None:
        import prism_sync

        assert "build_type_stats" not in prism_sync.__all__
        assert not hasattr(prism_sync, "build_type_stats")


# --------------------------------------------------------------------------
# Profile configuration errors vs gRPC runtime errors
# --------------------------------------------------------------------------


class TestProfileErrorsDoNotReachTheLookup:
    """Profile problems exit 3 before any remote lookup is attempted.

    ``VersionError`` covers both profile-configuration problems and gRPC
    failures, so the CLI must not classify ``VersionError`` by type alone.
    Profile errors are caught earlier, by ``select_profile``.
    """

    @pytest.mark.parametrize("command", ["--check", "--list-types"])
    def test_profile_missing_version_hash(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        """``_profile_from_dict`` rejects a profile without ``versionHash``."""
        path = _write(tmp_path, "p.json", {"profiles": {"a": {"gameVersion": "1"}}})
        calls: list[object] = []
        monkeypatch.setattr(
            cli_module,
            "resolve_remote_version",
            lambda *a, **k: calls.append(a) or VALID_VERSION,
        )
        _no_execution(monkeypatch)

        code = main(
            ["sync", command, "--output", str(tmp_path / "out"), "--profile", str(path)]
        )

        assert code == cli_module.EXIT_PROFILE
        assert "missing versionHash" in capsys.readouterr().err
        assert calls == [], "no remote lookup may happen for a bad profile"

    @pytest.mark.parametrize("command", ["--check", "--list-types"])
    def test_profile_missing_game_version(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        path = _write(tmp_path, "p.json", {"profiles": {"a": {"versionHash": "h"}}})
        _no_execution(monkeypatch)

        code = main(
            ["sync", command, "--output", str(tmp_path / "out"), "--profile", str(path)]
        )

        assert code == cli_module.EXIT_PROFILE
        assert "missing gameVersion" in capsys.readouterr().err

    @pytest.mark.parametrize("command", ["--check", "--list-types"])
    def test_profile_value_is_not_an_object(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        path = _write(tmp_path, "p.json", {"profiles": {"a": "nope"}})
        _no_execution(monkeypatch)

        code = main(
            ["sync", command, "--output", str(tmp_path / "out"), "--profile", str(path)]
        )

        assert code == cli_module.EXIT_PROFILE
        assert "must be a JSON object" in capsys.readouterr().err

    @pytest.mark.parametrize("command", ["--check", "--list-types"])
    def test_malformed_profile_json(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        path = tmp_path / "p.json"
        path.write_text("{not json", encoding="utf-8")
        _no_execution(monkeypatch)

        code = main(
            ["sync", command, "--output", str(tmp_path / "out"), "--profile", str(path)]
        )

        assert code == cli_module.EXIT_PROFILE
        assert "cannot parse profile file" in capsys.readouterr().err


class TestRuntimeErrorsStayDistinct:
    @pytest.mark.parametrize("command", ["--check", "--list-types"])
    def test_valid_profile_and_grpc_failure_exits_four(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        """A good profile plus a failing lookup is a runtime error, not 3."""
        path = _write(tmp_path, "p.json", PROFILE_OK)

        def boom(*args: object, **kwargs: object) -> str:
            raise VersionError("GetVersion failed after 1 attempt(s)")

        monkeypatch.setattr(cli_module, "resolve_remote_version", boom)
        _no_execution(monkeypatch)

        code = main(
            ["sync", command, "--output", str(tmp_path / "out"), "--profile", str(path)]
        )

        assert code == cli_module.EXIT_VERSION
        assert code != cli_module.EXIT_PROFILE
        assert "cannot resolve the remote version" in capsys.readouterr().err

    @pytest.mark.parametrize("command", ["--check", "--list-types"])
    def test_valid_profile_and_bad_version_exits_two(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        """A malformed explicit version is an input error even with a profile."""
        path = _write(tmp_path, "p.json", PROFILE_OK)
        _no_execution(monkeypatch)

        code = main(
            [
                "sync",
                command,
                "--output",
                str(tmp_path / "out"),
                "--profile",
                str(path),
                "--resource-version",
                "no-at-sign",
            ]
        )

        assert code == cli_module.EXIT_USAGE
        assert "invalid resource version" in capsys.readouterr().err

    def test_profile_error_wins_over_a_bad_version_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Profile validation runs first, so its error is reported first."""
        _no_execution(monkeypatch)

        code = main(
            [
                "sync",
                "--check",
                "--output",
                str(tmp_path / "out"),
                "--profile",
                str(tmp_path / "missing.json"),
                "--resource-version",
                "no-at-sign",
            ]
        )

        assert code == cli_module.EXIT_PROFILE
