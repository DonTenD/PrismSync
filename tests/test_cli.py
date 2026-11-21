"""Tests for the command-line entry point.

``sync`` parses its arguments and resolves a profile, then stops: the version
query and the actual synchronization land in later stages.  These tests pin
that contract so a later stage cannot silently start pretending to sync.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import ClassVar

import pytest

from prism_sync.cli import (
    EXIT_INTERRUPTED,
    EXIT_PARTIAL,
    EXIT_PROFILE,
    EXIT_SYNC,
    EXIT_USAGE,
    EXIT_VERSION,
    Console,
    ProfileSelectionError,
    build_parser,
    build_selection,
    main,
    select_profile,
)
from prism_sync.models import (
    CatalogAsset,
    CatalogEntry,
    ResourceType,
    SyncOutcome,
    SyncResult,
    VersionCheck,
    VersionProfile,
)
from prism_sync.version import VersionError

PROFILE_OK = {"gameVersion": "9.9.9", "versionHash": "test-hash"}


def _write(tmp_path: Path, name: str, payload: object) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _outcome(
    *,
    failed: int = 0,
    downloaded: int = 0,
    skipped: int = 0,
    resumed: int = 0,
    up_to_date: bool = False,
) -> SyncOutcome:
    """Build a ``SyncOutcome`` without touching the network or the disk."""
    check = VersionCheck(
        game_version="9.9.9",
        remote_resource_version="20006500@AAAA",
        remote_simple="20006500",
        local=None,
        up_to_date=up_to_date,
        reason="up to date" if up_to_date else "no local version marker",
    )
    if up_to_date:
        return SyncOutcome(check=check, result=None, marker_written=False)
    result = SyncResult(
        downloaded=downloaded,
        skipped=skipped,
        failed=failed,
        resumed=resumed,
        catalog_count=1,
        total_asset_count=downloaded + skipped + failed,
    )
    return SyncOutcome(check=check, result=result, marker_written=failed == 0)


class _FakeClient:
    """Records construction and closing so the lifecycle can be asserted."""

    instances: ClassVar[list[_FakeClient]] = []

    def __init__(self) -> None:
        self.closed = False
        self.entered = False
        _FakeClient.instances.append(self)

    def __enter__(self) -> _FakeClient:
        self.entered = True
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.closed = True


def _patch_sync(
    monkeypatch: pytest.MonkeyPatch,
    outcome: SyncOutcome | BaseException,
    *,
    remote: str | BaseException = "20006500@AAAA",
    captured: dict[str, object] | None = None,
) -> dict[str, object]:
    """Wire ``cli`` to a fake version lookup, client and sync entry point."""
    _FakeClient.instances = []
    recorded: dict[str, object] = {}

    def fake_resolve(profile: VersionProfile) -> str:
        recorded["profile"] = profile
        if isinstance(remote, Exception):
            raise remote
        return remote

    def fake_sync(client: object, version: str, **kwargs: object) -> SyncOutcome:
        recorded["version"] = version
        recorded["output"] = kwargs.get("output")
        recorded["game_version"] = kwargs.get("game_version")
        recorded["on_progress"] = kwargs.get("on_progress")
        recorded["selection"] = kwargs.get("selection")
        recorded["force"] = kwargs.get("force")
        recorded["verify"] = kwargs.get("verify")
        if "jobs" in kwargs:
            recorded["jobs"] = kwargs["jobs"]
        if captured is not None:
            captured.update(recorded)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr("prism_sync.cli.resolve_remote_version", fake_resolve)
    monkeypatch.setattr("prism_sync.cli.HttpClient", _FakeClient)
    monkeypatch.setattr("prism_sync.cli.sync_if_needed", fake_sync)
    return recorded


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


class TestParserShape:
    """The parser exposes exactly the documented surface."""

    def test_parser_has_sync_subcommand(self) -> None:
        """``sync`` parses its output directory."""
        parser = build_parser()
        args = parser.parse_args(["sync", "--output", "out"])
        assert args.command == "sync"
        assert args.output == "out"
        assert args.profile is None
        assert args.profile_name is None

    def test_output_is_required(self) -> None:
        """Omitting ``--output`` is a usage error."""
        parser = build_parser()
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["sync"])
        assert excinfo.value.code == EXIT_USAGE

    def test_profile_options_parse(self) -> None:
        """``--profile`` and ``--profile-name`` are accepted together."""
        parser = build_parser()
        args = parser.parse_args(
            ["sync", "--output", "o", "--profile", "p.json", "--profile-name", "b"]
        )
        assert args.profile == "p.json"
        assert args.profile_name == "b"

    def test_run_without_command_is_rejected(self) -> None:
        """No subcommand still parses, but ``main`` refuses to act."""
        assert build_parser().parse_args([]).command is None


class TestHelp:
    """``--help`` works at both levels."""

    def test_top_level_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Top-level help lists the ``sync`` command."""
        with pytest.raises(SystemExit) as excinfo:
            main(["--help"])
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "prism-sync" in out
        assert "sync" in out

    def test_sync_help_shows_options(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``sync --help`` documents all three options."""
        with pytest.raises(SystemExit) as excinfo:
            main(["sync", "--help"])
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        for flag in ("--output", "--profile", "--profile-name"):
            assert flag in out


# --------------------------------------------------------------------------
# Profile selection
# --------------------------------------------------------------------------


class TestSelectProfile:
    """How a document turns into a single profile."""

    def test_single_profile_document(self, tmp_path: Path) -> None:
        """A bare ``{gameVersion, versionHash}`` object is used directly."""
        path = _write(tmp_path, "single.json", PROFILE_OK)

        profile = select_profile(path, None)

        assert isinstance(profile, VersionProfile)
        assert profile.game_version == "9.9.9"

    def test_multiple_profiles_with_default(self, tmp_path: Path) -> None:
        """An explicit ``default`` is honoured."""
        path = _write(
            tmp_path,
            "multi.json",
            {
                "default": "beta",
                "profiles": {
                    "alpha": {"gameVersion": "1", "versionHash": "ha"},
                    "beta": {"gameVersion": "2", "versionHash": "hb"},
                },
            },
        )

        profile = select_profile(path, None)

        assert profile.game_version == "2"

    def test_multiple_profiles_without_default_requires_name(
        self, tmp_path: Path
    ) -> None:
        """Several profiles and no default is an error, not a silent pick."""
        path = _write(
            tmp_path,
            "multi.json",
            {
                "profiles": {
                    "alpha": {"gameVersion": "1", "versionHash": "ha"},
                    "beta": {"gameVersion": "2", "versionHash": "hb"},
                }
            },
        )

        with pytest.raises(ProfileSelectionError, match="pass --profile-name"):
            select_profile(path, None)

    def test_explicit_name_selects_profile(self, tmp_path: Path) -> None:
        """``--profile-name`` overrides the document default."""
        path = _write(
            tmp_path,
            "multi.json",
            {
                "default": "alpha",
                "profiles": {
                    "alpha": {"gameVersion": "1", "versionHash": "ha"},
                    "beta": {"gameVersion": "2", "versionHash": "hb"},
                },
            },
        )

        profile = select_profile(path, "beta")

        assert profile.game_version == "2"

    def test_unknown_name_lists_available(self, tmp_path: Path) -> None:
        """A wrong name reports the alternatives."""
        path = _write(
            tmp_path,
            "multi.json",
            {
                "profiles": {
                    "alpha": {"gameVersion": "1", "versionHash": "ha"},
                    "beta": {"gameVersion": "2", "versionHash": "hb"},
                }
            },
        )

        with pytest.raises(ProfileSelectionError, match=r"available: alpha, beta"):
            select_profile(path, "gamma")

    def test_missing_file(self, tmp_path: Path) -> None:
        """A missing document is an error."""
        with pytest.raises(ProfileSelectionError, match="profile file not found"):
            select_profile(tmp_path / "absent.json", None)

    def test_malformed_json(self, tmp_path: Path) -> None:
        """Unparsable JSON is an error."""
        path = tmp_path / "bad.json"
        path.write_text("not json", encoding="utf-8")

        with pytest.raises(ProfileSelectionError, match="cannot parse profile file"):
            select_profile(path, None)

    def test_missing_required_field(self, tmp_path: Path) -> None:
        """A profile without a version hash is rejected."""
        path = _write(tmp_path, "partial.json", {"gameVersion": "9.9.9"})

        with pytest.raises(ProfileSelectionError):
            select_profile(path, None)

    def test_directory_instead_of_file(self, tmp_path: Path) -> None:
        """A directory path fails instead of being treated as a document."""
        target = tmp_path / "adir"
        target.mkdir()

        with pytest.raises(ProfileSelectionError):
            select_profile(target, None)


# --------------------------------------------------------------------------
# main() behaviour
# --------------------------------------------------------------------------


class TestMainBehaviour:
    """Exit codes and messaging."""

    def test_no_command_prints_help_and_returns_usage(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Bare invocation prints usage to stderr."""
        assert main([]) == EXIT_USAGE
        assert "prism-sync" in capsys.readouterr().err

    def test_sync_success_returns_0(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A clean run reports the counts and exits with 0."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        _patch_sync(monkeypatch, _outcome(failed=0, downloaded=3, skipped=1))

        code = main(
            ["sync", "--all", "--output", "out", "--profile", str(profile_path)]
        )

        assert code == 0
        err = capsys.readouterr().err
        assert "downloaded 3" in err
        assert "skipped 1" in err
        assert "failed 0" in err

    def test_profile_error_returns_3(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Profile problems map to exit code 3."""
        code = main(
            [
                "sync",
                "--all",
                "--output",
                "out",
                "--profile",
                str(tmp_path / "absent.json"),
            ]
        )

        assert code == EXIT_PROFILE
        assert "profile file not found" in capsys.readouterr().err

    def test_ambiguous_profiles_return_3(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Several profiles without a default is a profile error."""
        path = _write(
            tmp_path,
            "multi.json",
            {
                "profiles": {
                    "a": {"gameVersion": "1", "versionHash": "ha"},
                    "b": {"gameVersion": "2", "versionHash": "hb"},
                }
            },
        )

        code = main(["sync", "--all", "--output", "out", "--profile", str(path)])

        assert code == EXIT_PROFILE
        assert "--profile-name" in capsys.readouterr().err

    def test_invalid_subcommand_exits_usage(self) -> None:
        """An unknown subcommand is rejected by argparse."""
        with pytest.raises(SystemExit) as excinfo:
            main(["nope"])
        assert excinfo.value.code == EXIT_USAGE


# --------------------------------------------------------------------------
# Sync execution (network and disk mocked out)
# --------------------------------------------------------------------------


class TestSyncFlow:
    """The CLI wires the library calls together in the right order."""

    def test_remote_version_is_queried_with_the_selected_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``resolve_remote_version`` receives the selected profile."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        recorded = _patch_sync(monkeypatch, _outcome(downloaded=1))

        main(["sync", "--all", "--output", "out", "--profile", str(profile_path)])

        profile = recorded["profile"]
        assert isinstance(profile, VersionProfile)
        assert profile.game_version == "9.9.9"

    def test_sync_receives_version_game_version_and_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The three values the library needs are forwarded verbatim."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        recorded = _patch_sync(monkeypatch, _outcome(downloaded=1))

        main(["sync", "--all", "--output", "assets", "--profile", str(profile_path)])

        assert recorded["version"] == "20006500@AAAA"
        assert recorded["game_version"] == "9.9.9"
        assert str(recorded["output"]).endswith("assets")

    def test_client_is_created_once_and_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exactly one client is built, entered and closed."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        _patch_sync(monkeypatch, _outcome(downloaded=1))

        main(["sync", "--all", "--output", "out", "--profile", str(profile_path)])

        assert len(_FakeClient.instances) == 1
        client = _FakeClient.instances[0]
        assert client.entered is True
        assert client.closed is True

    def test_client_closed_on_sync_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing sync still closes the client."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        _patch_sync(monkeypatch, RuntimeError("boom"))

        code = main(
            ["sync", "--all", "--output", "out", "--profile", str(profile_path)]
        )

        assert code == EXIT_SYNC
        assert _FakeClient.instances[0].closed is True

    def test_no_client_when_version_lookup_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed version lookup never opens a client."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        _patch_sync(monkeypatch, _outcome(), remote=VersionError("c010004"))

        code = main(
            ["sync", "--all", "--output", "out", "--profile", str(profile_path)]
        )

        assert code == EXIT_VERSION
        assert _FakeClient.instances == []


class TestSyncExitCodes:
    """Every failure mode maps to a distinct exit code."""

    def test_up_to_date_returns_0(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing to do still exits 0 and says so."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        _patch_sync(monkeypatch, _outcome(up_to_date=True))

        code = main(
            ["sync", "--all", "--output", "out", "--profile", str(profile_path)]
        )

        assert code == 0
        assert "already up to date" in capsys.readouterr().err

    def test_version_lookup_failure_returns_4(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A gRPC failure is exit code 4."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        _patch_sync(monkeypatch, _outcome(), remote=VersionError("force update"))

        code = main(
            ["sync", "--all", "--output", "out", "--profile", str(profile_path)]
        )

        assert code == EXIT_VERSION
        err = capsys.readouterr().err
        assert "cannot resolve the remote version" in err
        assert "force update" in err

    def test_sync_exception_returns_5(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unexpected sync error is exit code 5."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        _patch_sync(monkeypatch, RuntimeError("catalog exploded"))

        code = main(
            ["sync", "--all", "--output", "out", "--profile", str(profile_path)]
        )

        assert code == EXIT_SYNC
        err = capsys.readouterr().err
        assert "synchronization failed" in err
        assert "catalog exploded" in err

    def test_partial_failure_returns_6(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``failed > 0`` is exit 6 even though no exception escaped."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        _patch_sync(monkeypatch, _outcome(failed=2, downloaded=1))

        code = main(
            ["sync", "--all", "--output", "out", "--profile", str(profile_path)]
        )

        assert code == EXIT_PARTIAL
        assert "failed 2" in capsys.readouterr().err

    def test_profile_error_still_returns_3(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Profile problems are detected before any network work starts."""
        _patch_sync(monkeypatch, _outcome())

        code = main(
            [
                "sync",
                "--all",
                "--output",
                "out",
                "--profile",
                str(tmp_path / "absent.json"),
            ]
        )

        assert code == EXIT_PROFILE
        assert _FakeClient.instances == []


# --------------------------------------------------------------------------
# Console: quiet / verbose / TTY
# --------------------------------------------------------------------------


class _FakeStream:
    """In-memory stream with a configurable ``isatty`` answer."""

    def __init__(self, *, tty: bool) -> None:
        self.tty = tty
        self.chunks: list[str] = []

    def write(self, text: str) -> int:
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return self.tty

    @property
    def text(self) -> str:
        return "".join(self.chunks)


def _asset(name: str = "a.asset") -> CatalogAsset:
    entry = CatalogEntry(
        label=name,
        type_name="asset",
        size=1,
        checksum=1,
        seed=0,
        label_crc=2,
        priority=0,
        resource_type=ResourceType.RawPlain,
        resource_type_value=int(ResourceType.RawPlain),
    )
    return CatalogAsset(
        entry=entry,
        catalog_label="root",
        output_path=name,
        relative_path=name,
        real_name="deadbeef",
    )


class TestConsoleTTY:
    """Progress rendering differs between a terminal and a redirected log."""

    def test_tty_progress_uses_carriage_return(self) -> None:
        """On a TTY the line is rewritten in place and finished at 100%."""
        stream = _FakeStream(tty=True)
        console = Console(stream=stream)  # type: ignore[arg-type]

        console.progress(_asset(), 1, 2)
        console.progress(_asset(), 2, 2)

        text = stream.text
        assert "\r" in text
        assert "1/2" in text
        assert "2/2" in text
        assert text.endswith("\n")
        assert text.count("\n") == 1

    def test_non_tty_progress_is_coarse(self) -> None:
        """Without a TTY only milestone lines are printed, not one per asset."""
        stream = _FakeStream(tty=False)
        console = Console(stream=stream)  # type: ignore[arg-type]

        for done in range(1, 101):
            console.progress(_asset(), done, 100)

        lines = [line for line in stream.text.splitlines() if line]
        assert len(lines) <= 12
        assert any("100/100" in line for line in lines)
        assert "\r" not in stream.text

    def test_quiet_suppresses_progress(self) -> None:
        """``--quiet`` emits no progress at all."""
        stream = _FakeStream(tty=True)
        console = Console(quiet=True, stream=stream)  # type: ignore[arg-type]

        console.progress(_asset(), 1, 1)

        assert stream.text == ""


class TestConsoleModes:
    """Status, debug and error routing."""

    def test_status_suppressed_by_quiet(self) -> None:
        """Routine lines disappear under ``--quiet``."""
        stream = _FakeStream(tty=False)
        console = Console(quiet=True, stream=stream)  # type: ignore[arg-type]

        console.status("synchronizing")

        assert stream.text == ""

    def test_error_always_shown(self) -> None:
        """Errors survive ``--quiet``."""
        stream = _FakeStream(tty=False)
        console = Console(quiet=True, stream=stream)  # type: ignore[arg-type]

        console.error("boom")

        assert "boom" in stream.text

    def test_debug_requires_verbose(self) -> None:
        """Detail lines only appear with ``--verbose``."""
        quiet_stream = _FakeStream(tty=False)
        Console(stream=quiet_stream).debug("hidden")  # type: ignore[arg-type]
        assert quiet_stream.text == ""

        verbose_stream = _FakeStream(tty=False)
        Console(verbose=True, stream=verbose_stream).debug("shown")  # type: ignore[arg-type]
        assert "shown" in verbose_stream.text

    def test_quiet_and_verbose_are_mutually_exclusive(self) -> None:
        """argparse rejects the combination instead of picking one."""
        with pytest.raises(SystemExit) as excinfo:
            build_parser().parse_args(["sync", "--output", "o", "--quiet", "--verbose"])
        assert excinfo.value.code == EXIT_USAGE


# --------------------------------------------------------------------------
# Wiring of on_progress, quiet, verbose, interrupts
# --------------------------------------------------------------------------


class TestProgressWiring:
    """The callback handed to the library is the console's own."""

    def test_on_progress_is_forwarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``sync_if_needed`` receives a callable progress sink."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        captured: dict[str, object] = {}
        _patch_sync(monkeypatch, _outcome(downloaded=1), captured=captured)

        main(["sync", "--all", "--output", "out", "--profile", str(profile_path)])

        hook = captured.get("on_progress")
        assert callable(hook)

    def test_progress_callback_renders(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invoking the forwarded callback reaches the console."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        captured: dict[str, object] = {}
        _patch_sync(monkeypatch, _outcome(downloaded=1), captured=captured)

        main(["sync", "--all", "--output", "out", "--profile", str(profile_path)])

        hook = captured["on_progress"]
        assert callable(hook)
        hook(_asset("x.asset"), 1, 1)  # must not raise


class TestInterrupt:
    """Ctrl-C is reported tidily and still closes the client."""

    def test_keyboard_interrupt_returns_130(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An interrupt maps to 130 with no traceback."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        _patch_sync(monkeypatch, KeyboardInterrupt())

        code = main(
            ["sync", "--all", "--output", "out", "--profile", str(profile_path)]
        )

        assert code == EXIT_INTERRUPTED
        err = capsys.readouterr().err
        assert "interrupted" in err
        assert "Traceback" not in err

    def test_client_closed_after_interrupt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``with`` block still runs its exit path."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        _patch_sync(monkeypatch, KeyboardInterrupt())

        main(["sync", "--all", "--output", "out", "--profile", str(profile_path)])

        assert _FakeClient.instances[0].closed is True


class TestVerboseOutput:
    """``--verbose`` adds stage detail without changing results."""

    def test_verbose_prints_stages(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Game version, target and remote version are echoed."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        _patch_sync(monkeypatch, _outcome(downloaded=1))

        code = main(
            [
                "sync",
                "--all",
                "--output",
                "out",
                "--profile",
                str(profile_path),
                "--verbose",
            ]
        )

        assert code == 0
        err = capsys.readouterr().err
        assert "game version" in err
        assert "remote resource version" in err
        assert "gRPC target" in err

    def test_quiet_keeps_errors_only(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``--quiet`` drops the summary but still reports failures."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        _patch_sync(monkeypatch, _outcome(failed=1))

        code = main(
            [
                "sync",
                "--all",
                "--output",
                "out",
                "--profile",
                str(profile_path),
                "--quiet",
            ]
        )

        assert code == EXIT_PARTIAL
        err = capsys.readouterr().err
        assert "failed to download" in err
        assert "downloaded" not in err


class TestSummary:
    """The final line reports the ``SyncResult`` counters."""

    def test_summary_reports_counts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """All four counters appear."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        _patch_sync(monkeypatch, _outcome(downloaded=3, skipped=2, resumed=1, failed=0))

        main(["sync", "--all", "--output", "out", "--profile", str(profile_path)])

        err = capsys.readouterr().err
        for fragment in ("downloaded 3", "skipped 2", "failed 0", "resumed 1"):
            assert fragment in err


# --------------------------------------------------------------------------
# Filters and sync options (F-3-E)
# --------------------------------------------------------------------------


def _args(**overrides: object) -> argparse.Namespace:
    """Build a ``sync`` namespace with everything defaulted."""
    base = {
        "output": "out",
        "profile": None,
        "profile_name": None,
        "types": None,
        "max_size": None,
        "include": [],
        "exclude": [],
        "all": False,
        "jobs": None,
        "force": False,
        "verify": False,
        "quiet": False,
        "verbose": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class TestBuildSelection:
    """Filter flags map onto the library's ``Selection``."""

    def test_no_filters_is_inactive(self) -> None:
        """A bare command produces an inactive selection."""
        assert build_selection(_args()).active is False

    def test_types_uses_shared_parser(self) -> None:
        """``--types`` goes through ``parse_types`` (lowercased, split)."""
        selection = build_selection(_args(types="TXT, png"))

        assert selection.types == frozenset({"txt", "png"})
        assert selection.active is True

    def test_max_size_uses_shared_parser(self) -> None:
        """``--max-size`` goes through ``parse_size``."""
        selection = build_selection(_args(max_size="64KB"))

        assert selection.max_size == 64 * 1024

    def test_include_and_exclude_are_tuples(self) -> None:
        """Repeated globs become tuples."""
        selection = build_selection(_args(include=["a/*", "b/*"], exclude=["*.tmp"]))

        assert selection.include == ("a/*", "b/*")
        assert selection.exclude == ("*.tmp",)

    def test_combined_filters(self) -> None:
        """All four filters can be combined."""
        selection = build_selection(
            _args(types="txt", max_size="1KB", include=["x/*"], exclude=["y/*"])
        )

        assert selection.types == frozenset({"txt"})
        assert selection.max_size == 1024
        assert selection.include == ("x/*",)
        assert selection.exclude == ("y/*",)

    @pytest.mark.parametrize("bad", ["abc", "1XB", "GB"])
    def test_invalid_max_size_raises(self, bad: str) -> None:
        """A malformed size is rejected."""
        with pytest.raises(ValueError):
            build_selection(_args(max_size=bad))

    def test_empty_max_size_is_treated_as_absent(self) -> None:
        """An empty ``--max-size`` means "no size limit"."""
        assert build_selection(_args(max_size="")).max_size is None

    def test_empty_types_is_treated_as_absent(self) -> None:
        """An empty ``--types`` means "no type filter", not an error."""
        selection = build_selection(_args(types=""))

        assert selection.types is None
        assert selection.active is False

    @pytest.mark.parametrize("value", ["   ", " , "])
    def test_blank_types_raises(self, value: str) -> None:
        """A whitespace-only ``--types`` reaches the parser and is rejected."""
        with pytest.raises(ValueError, match="at least one value"):
            build_selection(_args(types=value))


class TestFullSyncGuard:
    """Running without a filter must be confirmed explicitly."""

    def test_no_filter_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The default is to refuse rather than start a 24 GB download."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        _patch_sync(monkeypatch, _outcome(downloaded=1))

        code = main(["sync", "--output", "out", "--profile", str(profile_path)])

        assert code == EXIT_USAGE
        err = capsys.readouterr().err
        assert "no filter given" in err
        assert "--all" in err
        assert _FakeClient.instances == []

    def test_all_allows_full_sync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--all`` confirms the intention and the run proceeds."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        recorded = _patch_sync(monkeypatch, _outcome(downloaded=1))

        code = main(
            ["sync", "--all", "--output", "out", "--profile", str(profile_path)]
        )

        assert code == 0
        selection = recorded["selection"]
        assert selection is not None
        assert selection.active is False

    def test_filter_alone_is_enough(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Any active filter removes the guard."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        _patch_sync(monkeypatch, _outcome(downloaded=1))

        code = main(
            [
                "sync",
                "--output",
                "out",
                "--profile",
                str(profile_path),
                "--max-size",
                "1KB",
            ]
        )

        assert code == 0

    def test_all_with_filter_still_passes_selection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--all`` plus a filter forwards the filter, it is not dropped."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        recorded = _patch_sync(monkeypatch, _outcome(downloaded=1))

        main(
            [
                "sync",
                "--all",
                "--output",
                "out",
                "--profile",
                str(profile_path),
                "--types",
                "txt",
            ]
        )

        selection = recorded["selection"]
        assert selection is not None
        assert selection.types == frozenset({"txt"})


class TestOptionForwarding:
    """``--jobs``/``--force``/``--verify`` reach the library unchanged."""

    def test_selection_is_forwarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The built selection is handed to ``sync_if_needed``."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        recorded = _patch_sync(monkeypatch, _outcome(downloaded=1))

        main(
            [
                "sync",
                "--output",
                "out",
                "--profile",
                str(profile_path),
                "--types",
                "txt",
                "--max-size",
                "2KB",
                "--include",
                "song/*",
                "--exclude",
                "*.bak",
            ]
        )

        selection = recorded["selection"]
        assert selection.types == frozenset({"txt"})
        assert selection.max_size == 2048
        assert selection.include == ("song/*",)
        assert selection.exclude == ("*.bak",)

    def test_jobs_force_verify_forwarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All three flags are forwarded verbatim."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        recorded = _patch_sync(monkeypatch, _outcome(downloaded=1))

        main(
            [
                "sync",
                "--output",
                "out",
                "--profile",
                str(profile_path),
                "--types",
                "txt",
                "--jobs",
                "3",
                "--force",
                "--verify",
            ]
        )

        assert recorded["jobs"] == 3
        assert recorded["force"] is True
        assert recorded["verify"] is True

    def test_defaults_are_not_overridden(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without the flags the library defaults are left in place."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        recorded = _patch_sync(monkeypatch, _outcome(downloaded=1))

        main(
            [
                "sync",
                "--output",
                "out",
                "--profile",
                str(profile_path),
                "--types",
                "txt",
            ]
        )

        assert "jobs" not in recorded
        assert recorded["force"] is False
        assert recorded["verify"] is False

    @pytest.mark.parametrize("jobs", [0, -1, 17, 99])
    def test_jobs_out_of_range_is_usage_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jobs: int
    ) -> None:
        """The library bound (1..16) is enforced before any work starts."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        _patch_sync(monkeypatch, _outcome(downloaded=1))

        code = main(
            [
                "sync",
                "--output",
                "out",
                "--profile",
                str(profile_path),
                "--jobs",
                str(jobs),
            ]
        )

        assert code == EXIT_USAGE
        assert _FakeClient.instances == []

    @pytest.mark.parametrize("jobs", [1, 8, 16])
    def test_jobs_in_range_is_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jobs: int
    ) -> None:
        """Boundary values inside the range are allowed."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        recorded = _patch_sync(monkeypatch, _outcome(downloaded=1))

        code = main(
            [
                "sync",
                "--output",
                "out",
                "--profile",
                str(profile_path),
                "--types",
                "txt",
                "--jobs",
                str(jobs),
            ]
        )

        assert code == 0
        assert recorded["jobs"] == jobs

    def test_invalid_filter_is_usage_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A malformed ``--max-size`` exits 2 with a clear message."""
        profile_path = _write(tmp_path, "p.json", PROFILE_OK)
        _patch_sync(monkeypatch, _outcome(downloaded=1))

        code = main(
            [
                "sync",
                "--output",
                "out",
                "--profile",
                str(profile_path),
                "--max-size",
                "bogus",
            ]
        )

        assert code == EXIT_USAGE
        assert "invalid filter" in capsys.readouterr().err
