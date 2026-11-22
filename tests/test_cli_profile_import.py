"""Tests for ``prism-sync profile import-device``.

Every case mocks the extractor; none of these is a real device test.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

import prism_sync.cli as cli_module
from prism_sync.cli import main
from prism_sync.hosttools import (
    AndroidHashResult,
    ExtractorInconclusive,
    ExtractorUnavailable,
)
from prism_sync.version import VersionError

HASH = "d157435ece8674102c7fbd4867ce593e26cc5d22"
OTHER_HASH = "0123456789abcdef0123456789abcdef01234567"


def _result(
    *,
    version_hash: str = HASH,
    game_version: str | None = "2.20.0",
    device: str | None = "Redmi K30 Pro Zoom Edition",
) -> AndroidHashResult:
    return AndroidHashResult(
        version_hash=version_hash, game_version=game_version, device=device
    )


def _patch_extract(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: AndroidHashResult | None = None,
    raises: BaseException | None = None,
) -> dict[str, Any]:
    """Replace the extractor call and record how it was invoked."""
    recorded: dict[str, Any] = {}

    def fake(command: Any, **kwargs: Any) -> AndroidHashResult:
        recorded["command"] = list(command)
        recorded["kwargs"] = kwargs
        if raises is not None:
            raise raises
        return result if result is not None else _result()

    monkeypatch.setattr(cli_module, "extract_android_version_hash", fake)
    return recorded


def _profile_file(tmp_path: Path, payload: dict[str, Any] | None = None) -> Path:
    path = tmp_path / "version_profiles.json"
    if payload is not None:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _argv(path: Path, *extra: str) -> list[str]:
    return [
        "profile",
        "import-device",
        "--profile",
        str(path),
        "--extractor",
        "prism-version-hash",
        "--yes",
        *extra,
    ]


# --------------------------------------------------------------------------
# happy paths
# --------------------------------------------------------------------------


class TestImportSuccess:
    def test_writes_a_new_profile(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _patch_extract(monkeypatch)
        path = _profile_file(tmp_path)

        code = main(_argv(path))

        assert code == 0
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["default"] == "2.20.0"
        assert data["profiles"]["2.20.0"]["gameVersion"] == "2.20.0"
        assert data["profiles"]["2.20.0"]["versionHash"] == HASH
        err = capsys.readouterr().err
        assert HASH in err
        assert "2.20.0" in err

    def test_reports_device_and_version(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _patch_extract(monkeypatch, result=_result(device="Test Device"))

        main(_argv(_profile_file(tmp_path)))

        err = capsys.readouterr().err
        assert "Test Device" in err

    def test_custom_profile_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_extract(monkeypatch)
        path = _profile_file(tmp_path)

        assert main(_argv(path, "--name", "latest")) == 0
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "latest" in data["profiles"]

    def test_extractor_project_shorthand(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded = _patch_extract(monkeypatch)
        path = _profile_file(tmp_path)

        code = main(
            [
                "profile",
                "import-device",
                "--profile",
                str(path),
                "--extractor-project",
                "/path/to/extractor",
                "--yes",
            ]
        )

        assert code == 0
        assert recorded["command"] == [
            "uv",
            "run",
            "--project",
            "/path/to/extractor",
            "prism-version-hash",
        ]

    def test_preserves_other_profiles(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_extract(monkeypatch)
        path = _profile_file(
            tmp_path,
            {
                "default": "2.19.1",
                "profiles": {
                    "2.19.1": {
                        "gameVersion": "2.19.1",
                        "versionHash": "f116097fd1d91dce705ad7604c3d044210389be8",
                    }
                },
            },
        )

        assert main(_argv(path)) == 0
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "2.19.1" in data["profiles"]
        assert data["profiles"]["2.19.1"]["versionHash"].startswith("f116097f")
        assert data["profiles"]["2.20.0"]["versionHash"] == HASH
        assert data["default"] == "2.19.1", "an existing default must survive"

    def test_timeout_is_forwarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded = _patch_extract(monkeypatch)

        main(_argv(_profile_file(tmp_path), "--timeout", "42"))

        assert recorded["kwargs"]["timeout"] == 42.0


# --------------------------------------------------------------------------
# extractor failures
# --------------------------------------------------------------------------


class TestExtractorFailures:
    def test_no_extractor_given(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        called: list[object] = []
        monkeypatch.setattr(
            cli_module,
            "extract_android_version_hash",
            lambda *a, **k: called.append(a),
        )
        path = _profile_file(tmp_path)

        code = main(["profile", "import-device", "--profile", str(path), "--yes"])

        assert code == cli_module.EXIT_USAGE
        assert called == [], "must not run the extractor without a command"
        assert not path.exists()

    def test_extractor_unavailable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _patch_extract(monkeypatch, raises=ExtractorUnavailable("device not connected"))
        path = _profile_file(tmp_path)

        code = main(_argv(path))

        assert code == cli_module.EXIT_VERSION
        assert "extractor unavailable" in capsys.readouterr().err
        assert not path.exists()

    def test_extractor_inconclusive(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _patch_extract(monkeypatch, raises=ExtractorInconclusive("confidence is low"))
        path = _profile_file(tmp_path)

        code = main(_argv(path))

        assert code == cli_module.EXIT_VERSION
        assert "rejected" in capsys.readouterr().err
        assert not path.exists()

    def test_game_version_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _patch_extract(monkeypatch, result=_result(game_version=None))
        path = _profile_file(tmp_path)

        code = main(_argv(path))

        assert code == cli_module.EXIT_VERSION
        assert "did not report a game version" in capsys.readouterr().err
        assert not path.exists(), "must not write an empty game version"

    def test_game_version_empty(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _patch_extract(monkeypatch, result=_result(game_version=""))
        path = _profile_file(tmp_path)

        code = main(_argv(path))

        assert code == cli_module.EXIT_VERSION
        assert not path.exists()


# --------------------------------------------------------------------------
# profile conflicts
# --------------------------------------------------------------------------


class TestConflicts:
    def test_same_name_requires_replace(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _patch_extract(monkeypatch)
        original = {
            "default": "2.20.0",
            "profiles": {
                "2.20.0": {"gameVersion": "2.20.0", "versionHash": OTHER_HASH}
            },
        }
        path = _profile_file(tmp_path, original)
        before = path.read_text(encoding="utf-8")

        code = main(_argv(path))

        assert code == cli_module.EXIT_PROFILE
        assert "already exists" in capsys.readouterr().err
        assert path.read_text(encoding="utf-8") == before, "file must be untouched"

    def test_replace_overwrites_the_same_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_extract(monkeypatch)
        path = _profile_file(
            tmp_path,
            {
                "default": "2.20.0",
                "profiles": {
                    "2.20.0": {"gameVersion": "2.20.0", "versionHash": OTHER_HASH}
                },
            },
        )

        assert main(_argv(path, "--replace")) == 0
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["profiles"]["2.20.0"]["versionHash"] == HASH

    def test_duplicate_hash_warns(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _patch_extract(monkeypatch)
        path = _profile_file(
            tmp_path,
            {
                "default": "old",
                "profiles": {"old": {"gameVersion": "2.20.0", "versionHash": HASH}},
            },
        )

        code = main(_argv(path, "--name", "fresh"))

        assert code == 0
        assert "already stored as profile" in capsys.readouterr().err

    def test_unreadable_existing_profile(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _patch_extract(monkeypatch)
        path = tmp_path / "version_profiles.json"
        path.write_text("{broken", encoding="utf-8")
        before = path.read_text(encoding="utf-8")

        code = main(_argv(path))

        assert code == cli_module.EXIT_PROFILE
        assert "cannot read" in capsys.readouterr().err
        assert path.read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------
# confirmation
# --------------------------------------------------------------------------


class TestConfirmation:
    def test_declined_write_leaves_file_untouched(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _patch_extract(monkeypatch)
        path = _profile_file(tmp_path)
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")

        code = main(
            [
                "profile",
                "import-device",
                "--profile",
                str(path),
                "--extractor",
                "prism-version-hash",
            ]
        )

        assert code == cli_module.EXIT_USAGE
        assert "aborted" in capsys.readouterr().err
        assert not path.exists()

    def test_accepted_write_proceeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_extract(monkeypatch)
        path = _profile_file(tmp_path)
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")

        code = main(
            [
                "profile",
                "import-device",
                "--profile",
                str(path),
                "--extractor",
                "prism-version-hash",
            ]
        )

        assert code == 0
        assert path.exists()

    def test_eof_on_stdin_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_extract(monkeypatch)
        path = _profile_file(tmp_path)

        def boom(prompt: str = "") -> str:
            raise EOFError

        monkeypatch.setattr("builtins.input", boom)

        assert (
            main(
                [
                    "profile",
                    "import-device",
                    "--profile",
                    str(path),
                    "--extractor",
                    "prism-version-hash",
                ]
            )
            == cli_module.EXIT_USAGE
        )
        assert not path.exists()


# --------------------------------------------------------------------------
# write failures
# --------------------------------------------------------------------------


class TestWriteFailure:
    def test_save_error_is_reported(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _patch_extract(monkeypatch)
        path = _profile_file(tmp_path)

        def boom(*args: object, **kwargs: object) -> None:
            raise VersionError("disk full")

        monkeypatch.setattr(cli_module, "save_profiles", boom)

        code = main(_argv(path))

        assert code == cli_module.EXIT_PROFILE
        assert "cannot write" in capsys.readouterr().err

    def test_os_error_is_reported(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _patch_extract(monkeypatch)
        path = _profile_file(tmp_path)

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("permission denied")

        monkeypatch.setattr(cli_module, "save_profiles", boom)

        assert main(_argv(path)) == cli_module.EXIT_PROFILE
        assert "cannot write" in capsys.readouterr().err


# --------------------------------------------------------------------------
# optional remote lookup
# --------------------------------------------------------------------------


class TestCheckRemote:
    def test_prints_resource_version(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _patch_extract(monkeypatch)
        monkeypatch.setattr(
            cli_module,
            "resolve_remote_version",
            lambda profile, **kwargs: "20006500@Y/Hfn4F3ipto1cz9Xl5Djro=",
        )
        path = _profile_file(tmp_path)

        code = main(_argv(path, "--check-remote"))

        assert code == 0
        assert "resource version" in capsys.readouterr().err

    def test_remote_failure_is_reported(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _patch_extract(monkeypatch)

        def boom(*args: object, **kwargs: object) -> str:
            raise VersionError("force update")

        monkeypatch.setattr(cli_module, "resolve_remote_version", boom)
        path = _profile_file(tmp_path)

        code = main(_argv(path, "--check-remote"))

        assert code == cli_module.EXIT_VERSION
        assert "remote lookup failed" in capsys.readouterr().err

    def test_remote_lookup_is_off_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_extract(monkeypatch)

        def boom(*args: object, **kwargs: object) -> str:
            raise AssertionError("gRPC must not run without --check-remote")

        monkeypatch.setattr(cli_module, "resolve_remote_version", boom)

        assert main(_argv(_profile_file(tmp_path))) == 0


# --------------------------------------------------------------------------
# sync must not touch the extractor
# --------------------------------------------------------------------------


class TestSyncIsIndependent:
    def test_sync_never_runs_the_extractor(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def boom(*args: object, **kwargs: object) -> object:
            raise AssertionError("sync must not start the Android extractor")

        monkeypatch.setattr(cli_module, "extract_android_version_hash", boom)
        monkeypatch.setattr(
            cli_module,
            "resolve_remote_version",
            lambda *a, **k: "20006500@Y/Hfn4F3ipto1cz9Xl5Djro=",
        )

        class _Client:
            def __init__(self, **kwargs: object) -> None: ...

            def __enter__(self) -> _Client:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(cli_module, "HttpClient", _Client)

        from prism_sync.sync import SyncOutcome

        monkeypatch.setattr(
            cli_module,
            "sync_if_needed",
            lambda *a, **k: SyncOutcome(
                result=None,
                downloaded=0,
                skipped=0,
                failed=0,
                resumed=0,
                catalogs=0,
                assets=0,
                pruned=0,
                unchanged=True,
            ),
        )

        code = main(
            [
                "sync",
                "--check",
                "--output",
                str(tmp_path / "out"),
                "--resource-version",
                "20006500@Y/Hfn4F3ipto1cz9Xl5Djro=",
                "--game-version",
                "2.20.0",
            ]
        )

        assert code in (0, 1)


# --------------------------------------------------------------------------
# stage 2-D: command prefix and check-remote failure messaging
# --------------------------------------------------------------------------


class TestConsolePrefix:
    """Each subcommand must label its own output."""

    def test_sync_prefix_unchanged(self) -> None:
        from prism_sync.cli import Console

        stream = io.StringIO()
        console = Console(stream=stream)
        console.status("hello")

        assert stream.getvalue().startswith("prism-sync sync: hello")

    def test_profile_import_prefix(self) -> None:
        from prism_sync.cli import Console

        stream = io.StringIO()
        console = Console(stream=stream, command="profile import-device")
        console.status("hello")

        assert stream.getvalue().startswith("prism-sync profile import-device: hello")

    @pytest.mark.parametrize(
        ("method", "args", "expected"),
        [
            ("status", ("msg",), "prism-sync profile import-device: msg"),
            ("debug", ("msg",), "prism-sync profile import-device: msg"),
            ("warning", ("msg",), "prism-sync profile import-device: warning: msg"),
            ("error", ("msg",), "prism-sync profile import-device: msg"),
            ("result", ("msg",), "prism-sync profile import-device: msg"),
        ],
    )
    def test_every_method_uses_the_label(
        self, method: str, args: tuple[str, ...], expected: str
    ) -> None:
        from prism_sync.cli import Console

        stream = io.StringIO()
        console = Console(stream=stream, command="profile import-device", verbose=True)
        getattr(console, method)(*args)

        assert expected in stream.getvalue()

    def test_progress_uses_the_label(self) -> None:
        from prism_sync.cli import Console
        from prism_sync.models import CatalogAsset, CatalogEntry

        asset = CatalogAsset(
            entry=CatalogEntry(
                label="root/raw/a.txt",
                type_name="txt",
                size=1,
                checksum=0,
                seed=0,
                label_crc=0,
                priority=0,
            ),
            catalog_label="root",
            output_path="",
            relative_path="raw/a.txt",
            real_name="a.txt",
        )
        stream = io.StringIO()
        console = Console(stream=stream, command="profile import-device")
        console.progress(asset, 10, 10)

        assert "prism-sync profile import-device: 10/10 (100%)" in stream.getvalue()

    def test_no_hard_coded_sync_prefix_remains(self) -> None:
        import inspect

        from prism_sync import cli as cli_module

        source = inspect.getsource(cli_module.Console)
        assert "{PROG} sync:" not in source, "Console must use self._label"


class TestCheckRemoteFailureKeepsProfile:
    def test_profile_survives_a_remote_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _patch_extract(monkeypatch)

        def boom(*args: object, **kwargs: object) -> str:
            raise VersionError("force update")

        monkeypatch.setattr(cli_module, "resolve_remote_version", boom)
        path = _profile_file(tmp_path)

        code = main(_argv(path, "--check-remote"))

        assert code == cli_module.EXIT_VERSION
        assert path.exists(), "the profile must not be rolled back"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["profiles"]["2.20.0"]["versionHash"] == HASH

    def test_failure_says_the_profile_was_saved(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _patch_extract(monkeypatch)

        def boom(*args: object, **kwargs: object) -> str:
            raise VersionError("force update")

        monkeypatch.setattr(cli_module, "resolve_remote_version", boom)
        path = _profile_file(tmp_path)

        main(_argv(path, "--check-remote"))

        err = capsys.readouterr().err
        assert "remote lookup failed" in err
        assert "was already saved to" in err
        assert str(path) in err
        assert "2.20.0" in err

    def test_failure_message_uses_the_new_prefix(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _patch_extract(monkeypatch)

        def boom(*args: object, **kwargs: object) -> str:
            raise VersionError("force update")

        monkeypatch.setattr(cli_module, "resolve_remote_version", boom)

        main(_argv(_profile_file(tmp_path), "--check-remote"))

        err = capsys.readouterr().err
        assert "prism-sync profile import-device:" in err
        assert "prism-sync sync:" not in err

    def test_profile_is_not_written_twice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_extract(monkeypatch)
        monkeypatch.setattr(
            cli_module,
            "resolve_remote_version",
            lambda *a, **k: (_ for _ in ()).throw(VersionError("boom")),
        )
        path = _profile_file(tmp_path)
        calls: list[object] = []
        real_save = cli_module.save_profiles

        def spy(*args: object, **kwargs: object) -> None:
            calls.append(args)
            real_save(*args, **kwargs)

        monkeypatch.setattr(cli_module, "save_profiles", spy)

        main(_argv(path, "--check-remote"))

        assert len(calls) == 1, "save_profiles must run exactly once"

    def test_normal_import_unaffected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _patch_extract(monkeypatch)
        path = _profile_file(tmp_path)

        code = main(_argv(path))

        assert code == 0
        err = capsys.readouterr().err
        assert "prism-sync profile import-device:" in err
        assert "already saved to" not in err
