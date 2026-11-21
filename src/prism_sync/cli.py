"""Command-line entry point for PrismSync.

``sync`` resolves a :class:`~prism_sync.models.VersionProfile`, queries the
remote resource version once, then runs the library's synchronization entry
point.  All terminal output funnels through :class:`Console` so the quiet,
verbose and non-interactive modes stay consistent.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from .http_client import HttpClient
from .models import MAX_JOBS, CatalogAsset, SyncOutcome, VersionProfile
from .selection import Selection, parse_size, parse_types
from .sync import sync_if_needed
from .version import (
    VersionError,
    load_profiles,
    resolve_profiles_path,
    resolve_remote_version,
)

PROG = "prism-sync"

#: Command line was invalid.
EXIT_USAGE = 2
#: The profile document is missing, malformed or cannot be selected.
EXIT_PROFILE = 3
#: The remote version could not be resolved (gRPC / version decoding).
EXIT_VERSION = 4
#: The synchronization itself failed.
EXIT_SYNC = 5
#: The synchronization ran but some assets failed.
EXIT_PARTIAL = 6
#: The selection would synchronize everything and ``--all`` was not given.
EXIT_NO_SELECTION = 2
#: The user interrupted the run.
EXIT_INTERRUPTED = 130


class ProfileSelectionError(Exception):
    """A profile document could not be turned into a single profile."""


class Console:
    """Route all CLI output, honouring the quiet/verbose/TTY settings.

    Attributes:
        quiet: Suppress progress and routine status lines.
        verbose: Emit extra detail about each stage.
        is_tty: Whether the destination supports carriage-return updates.
    """

    def __init__(
        self,
        quiet: bool = False,
        verbose: bool = False,
        *,
        stream: TextIO | None = None,
    ) -> None:
        """Create a console bound to ``stream`` (default: stderr)."""
        self.quiet = quiet
        self.verbose = verbose
        self._stream = stream if stream is not None else sys.stderr
        self.is_tty = _is_tty(self._stream)
        self._last_width = 0
        self._last_milestone = 0

    # -- plain messages ---------------------------------------------------

    def status(self, message: str) -> None:
        """Print a routine line unless ``--quiet`` was requested."""
        if self.quiet:
            return
        self._end_progress_line()
        print(f"{PROG} sync: {message}", file=self._stream)

    def debug(self, message: str) -> None:
        """Print detail only when ``--verbose`` was requested."""
        if not self.verbose:
            return
        self._end_progress_line()
        print(f"{PROG} sync: {message}", file=self._stream)

    def error(self, message: str) -> None:
        """Print an error; errors are never suppressed."""
        self._end_progress_line()
        print(f"{PROG} sync: {message}", file=self._stream)

    def result(self, message: str) -> None:
        """Print the final outcome; suppressed only under ``--quiet``."""
        if self.quiet:
            self._end_progress_line()
            return
        self._end_progress_line()
        print(f"{PROG} sync: {message}", file=self._stream)

    # -- progress ---------------------------------------------------------

    def progress(self, asset: CatalogAsset, done: int, total: int) -> None:
        """Report one finished asset.

        On a TTY the line is overwritten in place; otherwise it is emitted in
        coarse milestones so a redirected log does not grow one line per asset.
        """
        if self.quiet or total <= 0:
            return

        if self.is_tty:
            text = f"{done}/{total} {asset.relative_path}"
            if len(text) > 78:
                text = text[:75] + "..."
            padding = " " * max(0, self._last_width - len(text))
            self._stream.write(f"\r{text}{padding}")
            self._stream.flush()
            self._last_width = len(text)
            if done >= total:
                self._end_progress_line()
            return

        milestone = done * 100 // total
        if done >= total or milestone - self._last_milestone >= 10:
            self._last_milestone = milestone
            print(
                f"{PROG} sync: {done}/{total} ({milestone}%)",
                file=self._stream,
            )

    def _end_progress_line(self) -> None:
        """Finish an in-place progress line so later messages start clean."""
        if self.is_tty and self._last_width:
            self._stream.write("\n")
            self._stream.flush()
            self._last_width = 0


def _is_tty(stream: TextIO) -> bool:
    """Return whether ``stream`` looks like an interactive terminal."""
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``prism-sync`` command.

    Returns:
        The configured parser, with the ``sync`` subcommand attached.
    """
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Check, download and cache game resources.",
    )
    subcommands = parser.add_subparsers(dest="command", metavar="COMMAND")

    sync = subcommands.add_parser(
        "sync",
        help="synchronize resources into an output directory",
        description="Synchronize resources into an output directory.",
    )
    sync.add_argument(
        "--output",
        required=True,
        metavar="DIR",
        help="directory the resources are written to (required)",
    )
    sync.add_argument(
        "--profile",
        metavar="FILE",
        default=None,
        help=(
            "profile document to read; when omitted, the default lookup "
            "(./version_profiles.json, project root, user config) is used"
        ),
    )
    sync.add_argument(
        "--profile-name",
        metavar="NAME",
        default=None,
        help="profile to use when the document holds more than one",
    )
    sync.add_argument(
        "--types",
        metavar="LIST",
        default=None,
        help="comma separated resource types to keep, e.g. 'txt,png'",
    )
    sync.add_argument(
        "--max-size",
        metavar="SIZE",
        default=None,
        help="skip resources larger than this, e.g. '64KB' or '1.5MB'",
    )
    sync.add_argument(
        "--include",
        metavar="GLOB",
        action="append",
        default=[],
        help="keep only assets matching this pattern (repeatable)",
    )
    sync.add_argument(
        "--exclude",
        metavar="GLOB",
        action="append",
        default=[],
        help="drop assets matching this pattern (repeatable)",
    )
    sync.add_argument(
        "--all",
        action="store_true",
        help=(
            "synchronize every asset; required when no filter is given, "
            "since the full catalog is very large"
        ),
    )
    sync.add_argument(
        "--jobs",
        metavar="N",
        type=int,
        default=None,
        help="concurrent downloads (library default: 8)",
    )
    sync.add_argument(
        "--force",
        action="store_true",
        help="re-download even when the local file already looks valid",
    )
    sync.add_argument(
        "--verify",
        action="store_true",
        help="re-check assets whose transformed size cannot be derived",
    )
    noise = sync.add_mutually_exclusive_group()
    noise.add_argument(
        "--quiet",
        action="store_true",
        help="suppress progress and routine output; errors are still shown",
    )
    noise.add_argument(
        "--verbose",
        action="store_true",
        help="print extra detail about each stage",
    )
    return parser


# --------------------------------------------------------------------------
# Profile resolution
# --------------------------------------------------------------------------


def _has_explicit_default(document: object) -> bool:
    """Return whether ``document`` names a default profile explicitly."""
    if not isinstance(document, dict):
        return False
    return isinstance(document.get("default"), str)


def select_profile(
    path: str | Path | None,
    name: str | None,
) -> VersionProfile:
    """Resolve the profile the caller asked for.

    The document is read once to decide whether a choice is needed, then
    handed to :func:`~prism_sync.version.load_profiles` for validation.

    Args:
        path: Profile document path, or ``None`` to use the default lookup.
        name: Explicit profile name, or ``None`` to fall back to the
            document's own default.

    Returns:
        The selected profile.

    Raises:
        ProfileSelectionError: If the document is missing, malformed, holds
            several profiles without a default and no name was given, or the
            requested name does not exist.
    """
    resolved = resolve_profiles_path(path)

    try:
        raw = json.loads(Path(resolved).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProfileSelectionError(f"profile file not found: {resolved}") from exc
    except OSError as exc:
        raise ProfileSelectionError(
            f"cannot read profile file {resolved}: {exc}"
        ) from exc
    except Exception as exc:  # json.JSONDecodeError and unicode errors
        raise ProfileSelectionError(
            f"cannot parse profile file {resolved}: {exc}"
        ) from exc

    try:
        profiles, default_name = load_profiles(resolved)
    except VersionError as exc:
        raise ProfileSelectionError(str(exc)) from exc

    if name is not None:
        if name not in profiles:
            available = ", ".join(sorted(profiles))
            raise ProfileSelectionError(
                f"unknown profile {name!r} in {resolved}; available: {available}"
            )
        return profiles[name]

    if len(profiles) == 1:
        return next(iter(profiles.values()))

    if not _has_explicit_default(raw):
        available = ", ".join(sorted(profiles))
        raise ProfileSelectionError(
            f"{resolved} holds several profiles ({available}) and names no "
            "default; pass --profile-name"
        )

    return profiles[default_name]


# --------------------------------------------------------------------------
# Synchronization
# --------------------------------------------------------------------------


def _validate_jobs(jobs: int | None) -> str | None:
    """Return an error message when ``jobs`` is out of range, else ``None``."""
    if jobs is None:
        return None
    if not 1 <= jobs <= MAX_JOBS:
        return f"--jobs must be between 1 and {MAX_JOBS}, got {jobs}"
    return None


def build_selection(args: argparse.Namespace) -> Selection:
    """Turn the CLI filter flags into a :class:`~prism_sync.selection.Selection`.

    Args:
        args: Parsed ``sync`` arguments.

    Returns:
        The selection describing every filter the user supplied.

    Raises:
        ValueError: If ``--types`` or ``--max-size`` cannot be parsed.
    """
    types = parse_types(args.types) if args.types else None
    max_size = parse_size(args.max_size) if args.max_size else None
    return Selection(
        types=types,
        include=tuple(args.include),
        exclude=tuple(args.exclude),
        max_size=max_size,
    )


def run_sync(
    output: str,
    profile: VersionProfile,
    console: Console,
    *,
    selection: Selection | None = None,
    jobs: int | None = None,
    force: bool = False,
    verify: bool = False,
) -> int:
    """Resolve the remote version and synchronize into ``output``.

    The remote lookup happens once, then :func:`~prism_sync.sync.sync_if_needed`
    re-reads only the local marker: there is no second network round trip for
    the version.

    Args:
        output: Destination directory for the resources.
        profile: Version configuration used for the version query.
        console: Sink for all user-facing output.
        selection: Optional asset filter.
        jobs: Concurrent downloads; ``None`` keeps the library default.
        force: Re-download assets whose local file looks valid.
        verify: Re-check assets whose transformed size cannot be derived.

    Returns:
        Process exit status: ``0`` when up to date or fully synced, ``4`` when
        the remote version cannot be resolved, ``5`` when the sync itself
        fails, ``6`` when some assets failed and ``130`` when interrupted.
    """
    console.debug(f"game version {profile.game_version!r}")
    console.debug(f"gRPC target {profile.target}")

    try:
        remote = resolve_remote_version(profile)
    except VersionError as exc:
        console.error(f"cannot resolve the remote version: {exc}")
        return EXIT_VERSION
    console.debug(f"remote resource version {remote}")
    if selection is not None and selection.active:
        console.debug(f"selection {selection.to_dict()}")
    console.debug(f"jobs {jobs if jobs is not None else 'default'}")

    outcome: SyncOutcome | None = None
    try:
        with HttpClient() as client:
            console.status(f"synchronizing into {output}")
            options: dict[str, object] = {
                "game_version": profile.game_version,
                "output": Path(output),
                "selection": selection,
                "force": force,
                "verify": verify,
                "on_progress": console.progress,
            }
            if jobs is not None:
                options["jobs"] = jobs
            outcome = sync_if_needed(client, remote, **options)  # type: ignore[arg-type]
    except KeyboardInterrupt:
        console.error("interrupted; partial downloads were kept for resuming")
        return EXIT_INTERRUPTED
    except Exception as exc:
        console.error(f"synchronization failed: {exc}")
        return EXIT_SYNC

    return _report(outcome, console)


def _report(outcome: SyncOutcome, console: Console) -> int:
    """Print a summary of ``outcome`` and map it onto an exit code."""
    check = outcome.check
    if outcome.result is None:
        console.result(
            f"already up to date (remote version {check.remote_resource_version}, "
            f"reason: {check.reason})"
        )
        return 0

    result = outcome.result
    console.result(
        f"downloaded {result.downloaded}, skipped {result.skipped}, "
        f"failed {result.failed}, resumed {result.resumed} "
        f"({result.catalog_count} catalogs, "
        f"{result.total_asset_count} assets in catalog)"
    )
    if result.failed:
        console.error(f"{result.failed} asset(s) failed to download")
        return EXIT_PARTIAL
    if not outcome.marker_written:
        console.error("the run was incomplete; the version marker was not updated")
        return EXIT_PARTIAL
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface.

    Args:
        argv: Argument list to parse; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit status, documented in the module docstring and by the
        ``EXIT_*`` constants.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    if args.command == "sync":
        console = Console(quiet=args.quiet, verbose=args.verbose)
        console.debug(f"profile {args.profile!r}, profile name {args.profile_name!r}")

        try:
            selection = build_selection(args)
        except ValueError as exc:
            console.error(f"invalid filter: {exc}")
            return EXIT_USAGE

        if jobs_error := _validate_jobs(args.jobs):
            console.error(jobs_error)
            return EXIT_USAGE

        if not selection.active and not args.all:
            console.error(
                "no filter given: this would synchronize the entire catalog, "
                "which is very large; pass --types/--max-size/--include/"
                "--exclude to narrow it down, or --all to confirm"
            )
            return EXIT_USAGE

        try:
            profile = select_profile(args.profile, args.profile_name)
        except ProfileSelectionError as exc:
            console.error(str(exc))
            return EXIT_PROFILE

        return run_sync(
            args.output,
            profile,
            console,
            selection=selection,
            jobs=args.jobs,
            force=args.force,
            verify=args.verify,
        )

    parser.print_help(sys.stderr)  # pragma: no cover - argparse rejects these
    return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover - manual invocation
    raise SystemExit(main())
