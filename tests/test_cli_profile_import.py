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


# --------------------------------------------------------------------------
# profile add: interactive entry
# --------------------------------------------------------------------------


class TestProfileAdd:
    def _feed(self, monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
        """Make ``input`` return the given answers in order."""
        items = iter(answers)

        def fake(prompt: str = "") -> str:
            try:
                return next(items)
            except StopIteration:
                raise EOFError from None

        monkeypatch.setattr("builtins.input", fake)

    def test_prompts_for_both_values(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = _profile_file(tmp_path)
        self._feed(monkeypatch, ["2.20.0", HASH, "y"])

        code = main(["profile", "add", "--profile", str(path)])

        assert code == 0
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["profiles"]["2.20.0"]["gameVersion"] == "2.20.0"
        assert data["profiles"]["2.20.0"]["versionHash"] == HASH
        assert "prism-sync profile add:" in capsys.readouterr().err

    def test_options_skip_prompts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _profile_file(tmp_path)
        self._feed(monkeypatch, ["y"])

        code = main(
            [
                "profile",
                "add",
                "--profile",
                str(path),
                "--game-version",
                "2.20.0",
                "--version-hash",
                HASH,
            ]
        )

        assert code == 0
        assert (
            json.loads(path.read_text(encoding="utf-8"))["profiles"]["2.20.0"][
                "versionHash"
            ]
            == HASH
        )

    def test_hash_is_lowercased(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _profile_file(tmp_path)
        self._feed(monkeypatch, ["2.20.0", HASH.upper(), "y"])

        main(["profile", "add", "--profile", str(path)])

        assert (
            json.loads(path.read_text(encoding="utf-8"))["profiles"]["2.20.0"][
                "versionHash"
            ]
            == HASH
        )

    def test_rejects_a_bad_hash(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = _profile_file(tmp_path)
        self._feed(monkeypatch, ["2.20.0", "not-a-hash"])

        code = main(["profile", "add", "--profile", str(path)])

        assert code == cli_module.EXIT_USAGE
        assert "must be 40 hexadecimal" in capsys.readouterr().err
        assert not path.exists()

    def test_rejects_an_empty_game_version(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = _profile_file(tmp_path)
        self._feed(monkeypatch, ["", HASH])

        code = main(["profile", "add", "--profile", str(path)])

        assert code == cli_module.EXIT_USAGE
        assert "aborted" in capsys.readouterr().err
        assert not path.exists()

    def test_offers_existing_hashes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        old = "f116097fd1d91dce705ad7604c3d044210389be8"
        path = _profile_file(
            tmp_path,
            {
                "default": "2.19.1",
                "profiles": {"2.19.1": {"gameVersion": "2.19.1", "versionHash": old}},
            },
        )
        self._feed(monkeypatch, ["2.20.0", "1", "y"])

        main(["profile", "add", "--profile", str(path)])

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["profiles"]["2.20.0"]["versionHash"] == old
        assert old in capsys.readouterr().err

    def test_can_enter_a_new_hash_when_some_exist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old = "f116097fd1d91dce705ad7604c3d044210389be8"
        path = _profile_file(
            tmp_path,
            {
                "default": "2.19.1",
                "profiles": {"2.19.1": {"gameVersion": "2.19.1", "versionHash": old}},
            },
        )
        self._feed(monkeypatch, ["2.20.0", "2", HASH, "y"])

        main(["profile", "add", "--profile", str(path)])

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["profiles"]["2.20.0"]["versionHash"] == HASH
        assert data["profiles"]["2.19.1"]["versionHash"] == old
        assert data["default"] == "2.19.1"

    def test_cancel_at_prompt_writes_nothing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = _profile_file(tmp_path)
        self._feed(monkeypatch, [])

        code = main(["profile", "add", "--profile", str(path)])

        assert code == cli_module.EXIT_USAGE
        assert not path.exists()

    def test_declining_the_write_keeps_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _profile_file(tmp_path)
        self._feed(monkeypatch, ["2.20.0", HASH, "n"])

        code = main(["profile", "add", "--profile", str(path)])

        assert code == cli_module.EXIT_USAGE
        assert not path.exists()

    def test_existing_name_needs_replace(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = _profile_file(
            tmp_path,
            {
                "default": "2.20.0",
                "profiles": {
                    "2.20.0": {"gameVersion": "2.20.0", "versionHash": OTHER_HASH}
                },
            },
        )
        before = path.read_text(encoding="utf-8")
        self._feed(monkeypatch, ["2.20.0", "2", HASH])

        code = main(["profile", "add", "--profile", str(path)])

        assert code == cli_module.EXIT_PROFILE
        assert "already exists" in capsys.readouterr().err
        assert path.read_text(encoding="utf-8") == before

    def test_yes_skips_the_confirmation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _profile_file(tmp_path)
        self._feed(monkeypatch, ["2.20.0", HASH])

        code = main(["profile", "add", "--profile", str(path), "--yes"])

        assert code == 0
        assert path.exists()


# --------------------------------------------------------------------------
# interactive menu (no arguments on a TTY)
# --------------------------------------------------------------------------


class _FakeTTY(io.StringIO):
    """A stream that claims to be an interactive terminal."""

    def isatty(self) -> bool:
        return True


class TestInteractiveMenu:
    def _run(
        self, monkeypatch: pytest.MonkeyPatch, answers: list[str]
    ) -> tuple[int, str]:
        """Feed ``answers`` to the menu and return (exit code, stdout)."""
        items = iter(answers)
        out = io.StringIO()

        def fake_input(prompt: str = "") -> str:
            try:
                return next(items)
            except StopIteration:
                raise EOFError from None

        monkeypatch.setattr("builtins.input", fake_input)
        monkeypatch.setattr(cli_module.sys, "stdin", _FakeTTY())
        real_stdout = cli_module.sys.stdout
        monkeypatch.setattr(cli_module.sys, "stdout", out)
        try:
            code = main([])
        finally:
            monkeypatch.setattr(cli_module.sys, "stdout", real_stdout)
        return code, out.getvalue()

    def test_menu_is_shown_without_arguments(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, out = self._run(monkeypatch, ["q"])

        assert code == cli_module.EXIT_USAGE
        assert "choose an action" in out
        for label in ("sync", "check", "dry-run", "list-types", "profile-add"):
            assert label in out

    def test_quit_leaves_no_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "vp.json"
        self._run(monkeypatch, ["q"])

        assert not path.exists()

    def test_rejects_an_invalid_choice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code, out = self._run(monkeypatch, ["99", "q"])

        assert code == cli_module.EXIT_USAGE
        assert "invalid choice" in out

    def test_profile_add_flow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "vp.json"
        code, _ = self._run(
            monkeypatch,
            ["5", str(path), "2.20.0", HASH, "2.20.0", "y"],
        )

        assert code == 0
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["profiles"]["2.20.0"]["versionHash"] == HASH

    def test_profile_add_rejects_a_bad_hash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "vp.json"
        code, _ = self._run(monkeypatch, ["5", str(path), "2.20.0", "not-a-hash"])

        assert code == cli_module.EXIT_USAGE
        assert not path.exists()

    def test_list_types_asks_for_a_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _profile_file(tmp_path)
        calls: dict[str, object] = {}

        def fake_discover(client, version, **kwargs):
            calls.update(kwargs)
            return [], {}

        class _Client:
            def __init__(self, **kwargs: object) -> None: ...

            def __enter__(self) -> _Client:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(cli_module, "HttpClient", _Client)
        monkeypatch.setattr(cli_module, "discover_catalog_tree", fake_discover)

        code, _ = self._run(
            monkeypatch,
            ["4", str(tmp_path / "out"), str(path), "2.20.0", HASH],
        )

        assert code == 0
        assert calls["writable"] is False

    def test_sync_flow_reaches_the_downloader(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _profile_file(tmp_path)
        seen: dict[str, object] = {}

        def fake_sync(*args: object, **kwargs: object) -> object:
            seen.update(kwargs)
            from prism_sync.models import SyncOutcome, VersionCheck

            check = VersionCheck(
                game_version="2.20.0",
                remote_resource_version="20006500@Y/Hfn4F3ipto1cz9Xl5Djro=",
                remote_simple="20006500",
                local=None,
                up_to_date=True,
                reason="up to date",
            )
            return SyncOutcome(check=check, result=None, marker_written=False)

        monkeypatch.setattr(cli_module, "sync_if_needed", fake_sync)

        class _Client:
            def __init__(self, **kwargs: object) -> None: ...

            def __enter__(self) -> _Client:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(cli_module, "HttpClient", _Client)

        code, _ = self._run(
            monkeypatch,
            ["1", str(tmp_path / "out"), str(path), "2.20.0", HASH],
        )

        assert code == 0
        assert seen.get("game_version") == "2.20.0"

    def test_script_stdin_still_prints_help(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A non-TTY stdin must keep the old help output."""
        code = main([])

        assert code == cli_module.EXIT_USAGE
        assert "usage: prism-sync" in capsys.readouterr().err


class TestProjectRootDefaults:
    """Menu defaults point at the project root, not the process cwd."""

    def test_output_default_is_absolute_under_project_root(self) -> None:
        from prism_sync.version import project_root

        default = cli_module._default_output_dir()

        assert Path(default).is_absolute()
        assert Path(default) == project_root() / "assets"

    def test_profile_default_is_absolute_under_project_root(self) -> None:
        from prism_sync.version import project_root

        default = cli_module._default_profile_path()

        assert Path(default).is_absolute()
        assert Path(default) == project_root() / "version_profiles.json"

    def test_cwd_does_not_affect_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = cli_module._default_output_dir()
        monkeypatch.chdir(tmp_path)

        assert cli_module._default_output_dir() == before


class TestExistingResourceNotes:
    def test_missing_output_dir_has_no_note(self, tmp_path: Path) -> None:
        assert cli_module._describe_existing_output(tmp_path / "absent") is None

    def test_empty_output_dir_is_reported(self, tmp_path: Path) -> None:
        (tmp_path / "empty").mkdir()

        note = cli_module._describe_existing_output(tmp_path / "empty")

        assert note is not None and "empty" in note

    def test_populated_output_dir_is_reported(self, tmp_path: Path) -> None:
        target = tmp_path / "used"
        target.mkdir()
        (target / "keep.txt").write_text("x", encoding="utf-8")

        note = cli_module._describe_existing_output(target)

        assert note is not None and "not empty" in note

    def test_previous_sync_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "synced"
        target.mkdir()
        (target / "sync-state.json").write_text(
            json.dumps(
                {"resourceVersion": "20006500@Y/abc=", "files": {"a.txt": {"size": 1}}}
            ),
            encoding="utf-8",
        )

        note = cli_module._describe_existing_output(target)

        assert note is not None
        assert "1 recorded files" in note
        assert "20006500@Y/abc=" in note

    def test_unreadable_state_is_reported(self, tmp_path: Path) -> None:
        target = tmp_path / "broken"
        target.mkdir()
        (target / "sync-state.json").write_text("{bad", encoding="utf-8")

        note = cli_module._describe_existing_output(target)

        assert note is not None and "unreadable" in note

    def test_existing_profiles_are_listed(self, tmp_path: Path) -> None:
        path = _profile_file(
            tmp_path,
            {
                "default": "b",
                "profiles": {
                    "a": {"gameVersion": "1", "versionHash": OTHER_HASH},
                    "b": {"gameVersion": "2", "versionHash": HASH},
                },
            },
        )
        console = cli_module.Console(quiet=True, stream=io.StringIO())

        note = cli_module._describe_existing_profile(path, console)

        assert note is not None
        assert "2 profile(s)" in note
        assert "a, b" in note

    def test_missing_profiles_file_has_no_note(self, tmp_path: Path) -> None:
        console = cli_module.Console(quiet=True, stream=io.StringIO())

        assert (
            cli_module._describe_existing_profile(tmp_path / "absent.json", console)
            is None
        )


class TestMenuCancellation:
    """Cancelling returns to the menu; only ``q`` leaves."""

    def _run(self, monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> int:
        items = iter(answers)

        def fake_input(prompt: str = "") -> str:
            try:
                return next(items)
            except StopIteration:
                raise EOFError from None

        # Do not read the developer's own profile from the repository root.
        monkeypatch.setattr(
            cli_module, "_default_profile_path", lambda: "/nonexistent/vp.json"
        )
        monkeypatch.setattr(
            cli_module, "_default_output_dir", lambda: "/nonexistent/assets"
        )
        monkeypatch.setattr("builtins.input", fake_input)
        monkeypatch.setattr(cli_module.sys, "stdin", _FakeTTY())
        monkeypatch.setattr(cli_module.sys, "stdout", io.StringIO())
        return main([])

    def test_cancel_returns_to_menu(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An empty gameVersion does not kill the session."""
        code = self._run(monkeypatch, ["2", str(tmp_path / "out"), "", "", "q"])

        assert code == cli_module.EXIT_USAGE
        err = capsys.readouterr().err
        assert "no gameVersion is known yet" in err
        assert "back to the menu" in err

    def test_repeats_after_cancel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After cancelling, a second choice is still possible."""
        (tmp_path / "out").mkdir()
        monkeypatch.setattr(
            cli_module,
            "_menu_pick",
            lambda: "list_types",
        )
        calls: list[int] = []

        def fake_list(output: object, profile: object, console: object) -> int:
            calls.append(1)
            return 0

        monkeypatch.setattr(cli_module, "run_list_types", fake_list)
        monkeypatch.setattr(
            cli_module,
            "_menu_ask_profile",
            lambda *a, **k: None,  # always cancelled
        )
        monkeypatch.setattr(
            cli_module,
            "_menu_pick",
            iter(["list_types", "list_types", None]).__next__,
        )
        monkeypatch.setattr(cli_module, "_ask", lambda *a, **k: str(tmp_path))

        code = self._run(monkeypatch, [])

        assert code == cli_module.EXIT_USAGE
        assert calls == [], "a cancelled action must not run the command"

    def test_missing_version_explains_why(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """With no stored version the prompt says it is required."""
        sink = io.StringIO()
        console = cli_module.Console(stream=sink)
        items = iter(["", HASH])
        monkeypatch.setattr("builtins.input", lambda p="": next(items, None))

        result = cli_module._menu_ask_profile(console, str(tmp_path / "vp.json"))

        assert result is None
        err = sink.getvalue()
        assert "no gameVersion is known yet" in err
        assert "back to the menu" in err

    def test_stored_version_is_offered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _profile_file(
            tmp_path,
            {
                "default": "2.19.1",
                "profiles": {"2.19.1": {"gameVersion": "2.19.1", "versionHash": HASH}},
            },
        )
        console = cli_module.Console(quiet=True, stream=io.StringIO())
        items = iter(["", "1"])
        monkeypatch.setattr("builtins.input", lambda p="": next(items, None))

        result = cli_module._menu_ask_profile(console, str(path))

        assert result is not None
        assert result.game_version == "2.19.1"
        assert result.version_hash == HASH


class TestMenuPersistsProfile:
    """Values typed in the menu are offered to be saved."""

    def test_saves_a_new_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "vp.json"
        sink = io.StringIO()
        console = cli_module.Console(stream=sink)
        monkeypatch.setattr("builtins.input", lambda p="": "y")

        cli_module._offer_profile_save(
            console,
            str(path),
            _profile_file(tmp_path) and cli_module.VersionProfile("2.20.0", HASH),
        )

        assert path.is_file()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["profiles"]["2.20.0"]["versionHash"] == HASH
        assert data["default"] == "2.20.0"

    def test_declining_keeps_no_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "vp.json"
        console = cli_module.Console(quiet=True, stream=io.StringIO())
        monkeypatch.setattr("builtins.input", lambda p="": "n")

        cli_module._offer_profile_save(
            console, str(path), cli_module.VersionProfile("2.20.0", HASH)
        )

        assert not path.exists()

    def test_keeps_existing_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _profile_file(
            tmp_path,
            {
                "default": "2.19.1",
                "profiles": {
                    "2.19.1": {"gameVersion": "2.19.1", "versionHash": OTHER_HASH}
                },
            },
        )
        console = cli_module.Console(quiet=True, stream=io.StringIO())
        monkeypatch.setattr("builtins.input", lambda p="": "y")

        cli_module._offer_profile_save(
            console, str(path), cli_module.VersionProfile("2.20.0", HASH)
        )

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["profiles"]["2.19.1"]["versionHash"] == OTHER_HASH
        assert data["profiles"]["2.20.0"]["versionHash"] == HASH
        assert data["default"] == "2.19.1"

    def test_same_hash_is_not_rewritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _profile_file(
            tmp_path,
            {
                "default": "2.20.0",
                "profiles": {"2.20.0": {"gameVersion": "2.20.0", "versionHash": HASH}},
            },
        )
        before = path.read_text(encoding="utf-8")
        sink = io.StringIO()
        console = cli_module.Console(stream=sink)

        cli_module._offer_profile_save(
            console, str(path), cli_module.VersionProfile("2.20.0", HASH)
        )

        assert path.read_text(encoding="utf-8") == before
        assert "save this profile" not in sink.getvalue()

    def test_conflicting_hash_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _profile_file(
            tmp_path,
            {
                "default": "2.20.0",
                "profiles": {
                    "2.20.0": {"gameVersion": "2.20.0", "versionHash": OTHER_HASH}
                },
            },
        )
        before = path.read_text(encoding="utf-8")
        sink = io.StringIO()
        console = cli_module.Console(stream=sink)

        cli_module._offer_profile_save(
            console, str(path), cli_module.VersionProfile("2.20.0", HASH)
        )

        assert path.read_text(encoding="utf-8") == before
        assert "different hash" in sink.getvalue()
