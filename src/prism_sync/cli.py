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
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from .catalog_client import discover_catalog_tree
from .catalog_stats import _build_type_stats
from .downloader import AssetDownloader
from .hosttools import (
    ExtractorInconclusive,
    ExtractorUnavailable,
    extract_android_version_hash,
)
from .http_client import HttpClient
from .manifest import decode_full_version
from .models import MAX_JOBS, CatalogAsset, SyncOutcome, VersionProfile
from .plan import build_sync_plan
from .selection import Selection, parse_size, parse_types
from .state import SyncStateStore
from .sync import DEFAULT_STATE_FILE, sync_if_needed
from .version import (
    VersionError,
    check_version,
    load_profiles,
    resolve_profiles_path,
    resolve_remote_version,
    save_profiles,
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
#: ``--check`` found a newer remote version.
EXIT_UPDATE_AVAILABLE = 1
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
        command: str = "sync",
    ) -> None:
        """Create a console bound to ``stream`` (default: stderr).

        Args:
            quiet: Suppress progress and routine status lines.
            verbose: Emit extra detail about each stage.
            stream: Destination stream; defaults to standard error.
            command: Subcommand label used as the message prefix.
        """
        self.quiet = quiet
        self.verbose = verbose
        self._stream = stream if stream is not None else sys.stderr
        self._label = f"{PROG} {command}:"
        self.is_tty = _is_tty(self._stream)
        self._last_width = 0
        self._last_milestone = 0

    # -- plain messages ---------------------------------------------------

    def status(self, message: str) -> None:
        """Print a routine line unless ``--quiet`` was requested."""
        if self.quiet:
            return
        self._end_progress_line()
        print(f"{self._label} {message}", file=self._stream)

    def debug(self, message: str) -> None:
        """Print detail only when ``--verbose`` was requested."""
        if not self.verbose:
            return
        self._end_progress_line()
        print(f"{self._label} {message}", file=self._stream)

    def warning(self, message: str) -> None:
        """Print a warning; suppressed only under ``--quiet``."""
        if self.quiet:
            return
        self._end_progress_line()
        print(f"{self._label} warning: {message}", file=self._stream)

    def error(self, message: str) -> None:
        """Print an error; errors are never suppressed."""
        self._end_progress_line()
        print(f"{self._label} {message}", file=self._stream)

    def result(self, message: str) -> None:
        """Print the final outcome; suppressed only under ``--quiet``."""
        if self.quiet:
            self._end_progress_line()
            return
        self._end_progress_line()
        print(f"{self._label} {message}", file=self._stream)

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
                f"{self._label} {done}/{total} ({milestone}%)",
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
        "--game-version",
        metavar="VERSION",
        default=None,
        help=(
            "game version used for the root catalog CRC; required with "
            "--resource-version when the profile would otherwise be skipped"
        ),
    )
    sync.add_argument(
        "--resource-version",
        metavar="VERSION",
        default=None,
        help=(
            "use this '<simple>@<base64>' resource version directly and skip "
            "the gRPC lookup"
        ),
    )
    sync.add_argument(
        "--check",
        action="store_true",
        help=(
            "compare the remote version with the local marker and exit; "
            "downloads nothing"
        ),
    )
    sync.add_argument(
        "--dry-run",
        action="store_true",
        help=("report what a sync would add, change or remove; writes nothing"),
    )
    sync.add_argument(
        "--list-types",
        action="store_true",
        help=("fetch the catalog and print per-type totals; downloads no assets"),
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
    sync.add_argument(
        "--connect-timeout",
        metavar="SECONDS",
        type=float,
        default=None,
        help="HTTP connect timeout (library default: 10.0)",
    )
    sync.add_argument(
        "--read-timeout",
        metavar="SECONDS",
        type=float,
        default=None,
        help="HTTP read/write timeout (library default: 60.0)",
    )
    sync.add_argument(
        "--retries",
        metavar="N",
        type=int,
        default=None,
        help="HTTP retry attempts (library default: 3)",
    )
    sync.add_argument(
        "--base-url",
        metavar="URL",
        default=None,
        help="override the CDN base URL",
    )
    sync.add_argument(
        "--no-proxy",
        action="store_true",
        help="ignore proxy environment variables",
    )
    sync.add_argument(
        "--grpc-timeout",
        metavar="SECONDS",
        type=float,
        default=None,
        help="gRPC version query timeout (library default: 10.0)",
    )
    sync.add_argument(
        "--grpc-attempts",
        metavar="N",
        type=int,
        default=None,
        help="gRPC version query attempts (library default: 3)",
    )

    profile = subcommands.add_parser(
        "profile",
        help="manage the version profile document",
        description="Manage the version profile document.",
    )
    profile_commands = profile.add_subparsers(dest="profile_command", metavar="ACTION")

    import_device = profile_commands.add_parser(
        "import-device",
        help="read the version hash from a connected Android device",
        description=(
            "Extract the version hash from a running Android game and store it "
            "in the profile document. Requires a separate extractor tool, a "
            "rooted device and a running Frida server."
        ),
    )
    import_device.add_argument(
        "--profile",
        metavar="FILE",
        required=True,
        help="profile document to update (created when missing)",
    )
    import_device.add_argument(
        "--name",
        metavar="NAME",
        default=None,
        help="profile name to write (default: the game version)",
    )
    import_device.add_argument(
        "--extractor",
        metavar="CMD",
        action="append",
        default=None,
        help=(
            "extractor command; repeat for each argument, e.g. "
            "--extractor uv --extractor run --extractor prism-version-hash"
        ),
    )
    import_device.add_argument(
        "--extractor-project",
        metavar="DIR",
        default=None,
        help=(
            "PrismVersionHashExtractor project directory; implies "
            "'uv run --project DIR prism-version-hash'"
        ),
    )
    import_device.add_argument(
        "--timeout",
        metavar="SECONDS",
        type=float,
        default=300.0,
        help="extractor timeout (default: 300)",
    )
    import_device.add_argument(
        "--replace",
        action="store_true",
        help="overwrite an existing profile with the same name",
    )
    import_device.add_argument(
        "--yes",
        action="store_true",
        help="write without asking for confirmation",
    )
    import_device.add_argument(
        "--check-remote",
        action="store_true",
        help="also query gRPC with the imported hash and show the resource version",
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


@dataclass(frozen=True, slots=True)
class ConnectionOptions:
    """Network settings gathered from the CLI flags.

    Every field is ``None`` (or ``False`` for ``no_proxy``) when the caller did
    not override it, so the library defaults still apply.
    """

    base_url: str | None = None
    connect_timeout: float | None = None
    read_timeout: float | None = None
    retries: int | None = None
    no_proxy: bool = False
    grpc_timeout: float | None = None
    grpc_attempts: int | None = None

    def client_kwargs(self) -> dict[str, object]:
        """Return the keyword arguments ``HttpClient`` should be built with."""
        kwargs: dict[str, object] = {}
        if self.base_url is not None:
            kwargs["base_url"] = self.base_url
        if self.connect_timeout is not None:
            kwargs["connect_timeout"] = self.connect_timeout
        if self.read_timeout is not None:
            kwargs["read_timeout"] = self.read_timeout
        if self.retries is not None:
            kwargs["retries"] = self.retries
        if self.no_proxy:
            kwargs["trust_env"] = False
        return kwargs

    def version_kwargs(self) -> dict[str, object]:
        """Return the keyword arguments ``resolve_remote_version`` accepts."""
        kwargs: dict[str, object] = {}
        if self.grpc_timeout is not None:
            kwargs["timeout"] = self.grpc_timeout
        if self.grpc_attempts is not None:
            kwargs["attempts"] = self.grpc_attempts
        return kwargs


def validate_connection(options: ConnectionOptions) -> str | None:
    """Return an error message for a bad connection option, else ``None``."""
    for name, value in (
        ("--connect-timeout", options.connect_timeout),
        ("--read-timeout", options.read_timeout),
        ("--grpc-timeout", options.grpc_timeout),
    ):
        if value is not None and value <= 0:
            return f"{name} must be positive, got {value}"
    if options.retries is not None and options.retries < 0:
        return f"--retries must be non-negative, got {options.retries}"
    if options.grpc_attempts is not None and options.grpc_attempts < 1:
        return f"--grpc-attempts must be at least 1, got {options.grpc_attempts}"
    if options.base_url is not None and not _is_valid_url(options.base_url):
        return f"--base-url is not a valid http(s) URL: {options.base_url!r}"
    return None


def _is_valid_url(value: str) -> bool:
    """Return whether ``value`` is an absolute http(s) URL with a host."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _profile_for_explicit_version(
    args: argparse.Namespace,
    console: Console,
) -> VersionProfile | None:
    """Build the profile used together with ``--resource-version``.

    The resource version is supplied directly, so gRPC is skipped and the
    profile only supplies ``game_version`` for the root catalog CRC.  Explicit
    ``--profile`` / ``--profile-name`` are never silently ignored: they are
    still validated, and ``--game-version`` overrides the file's value.

    Args:
        args: Parsed ``sync`` arguments.
        console: Sink for warnings and errors.

    Returns:
        The profile to use, or ``None`` when the supplied profile is invalid.
    """
    explicit_profile = args.profile is not None or args.profile_name is not None
    file_game_version: str | None = None

    if explicit_profile:
        try:
            loaded = select_profile(args.profile, args.profile_name)
        except ProfileSelectionError as exc:
            console.error(str(exc))
            return None
        file_game_version = loaded.game_version
        console.debug("validated the supplied profile (--resource-version)")
    else:
        console.debug("skipping the profile lookup (--resource-version)")

    game_version = args.game_version or file_game_version or ""
    if not game_version:
        console.warning(
            "no --game-version given; the root catalog CRC will be derived "
            "from an empty game version, which usually fails. Pass "
            "--game-version or a profile with gameVersion."
        )
    return VersionProfile(game_version=game_version, version_hash="")


class _InvalidInput:
    """Sentinel returned by :func:`_resolve_remote` for a malformed version."""


_INVALID_INPUT = _InvalidInput()


def _resolve_remote(
    profile: VersionProfile,
    console: Console,
    *,
    resource_version: str | None,
    connection: ConnectionOptions,
) -> str | _InvalidInput | None:
    """Resolve the remote version, reporting CLI problems on ``console``.

    Returns:
        The resolved version, ``None`` when the remote lookup failed
        (:data:`EXIT_VERSION`) or :data:`_INVALID_INPUT` when the supplied
        resource version is malformed (:data:`EXIT_USAGE`).
    """
    if resource_version is not None:
        # Validate the user-supplied string up front instead of inferring the
        # source of a ValueError from the exception type alone.
        try:
            decode_full_version(resource_version)
        except ValueError as exc:
            console.error(f"invalid resource version: {exc}")
            return _INVALID_INPUT

    try:
        return resolve_remote_version(
            profile,
            resource_version=resource_version,
            **connection.version_kwargs(),  # type: ignore[arg-type]
        )
    except VersionError as exc:
        console.error(f"cannot resolve the remote version: {exc}")
        return None


def _read_only_conflict(args: argparse.Namespace) -> str | None:
    """Return an error message for flags a read-only command cannot honour.

    ``--force`` and an explicit ``--jobs`` only make sense while downloading.
    ``--check`` additionally compares a global version, so resource filters
    would be silently ignored and are rejected instead.
    """
    if args.force:
        flag = "--check" if args.check else "--list-types"
        return f"{flag} downloads nothing; --force is not supported"
    if args.jobs is not None:
        flag = "--check" if args.check else "--list-types"
        return f"{flag} downloads nothing; --jobs is not supported"
    if args.check and _selection_requested(args):
        return (
            "--check is a global version check and does not support resource "
            "filters (--types, --include, --exclude, --max-size)"
        )
    return None


def _selection_requested(args: argparse.Namespace) -> bool:
    """Report whether any resource filter flag was given explicitly."""
    return bool(
        args.types is not None
        or args.max_size is not None
        or args.include
        or args.exclude
    )


def run_check(
    output: str,
    profile: VersionProfile,
    console: Console,
    *,
    selection: Selection | None = None,
    resource_version: str | None = None,
    connection: ConnectionOptions | None = None,
) -> int:
    """Compare the remote version with the local marker and stop.

    Nothing is downloaded and neither the version marker nor the state
    snapshot is written.

    Returns:
        ``0`` when up to date, ``1`` when an update is available, ``4`` when
        the remote version cannot be resolved or the version string is invalid.
    """
    conn = connection if connection is not None else ConnectionOptions()
    remote = _resolve_remote(
        profile,
        console,
        resource_version=resource_version,
        connection=conn,
    )
    if remote is None:
        return EXIT_VERSION
    if isinstance(remote, _InvalidInput):
        return EXIT_USAGE

    check = check_version(profile, output, remote, selection)
    local = check.local
    console.result(f"remote resource version: {check.remote_resource_version}")
    if local is None:
        console.result("local resource version: (none)")
        console.result("status: update-available (no local version marker)")
        return EXIT_UPDATE_AVAILABLE

    console.result(f"local resource version: {local.resource_version}")
    if check.up_to_date:
        console.result("status: up-to-date")
        return 0
    console.result(f"status: update-available ({check.reason})")
    return EXIT_UPDATE_AVAILABLE


def _catalog_error(console: Console, exc: Exception) -> int:
    """Report a catalog/planning failure and return the shared exit code."""
    console.error(f"cannot read the catalog: {exc}")
    return EXIT_SYNC


def run_list_types(
    output: str,
    profile: VersionProfile,
    console: Console,
    *,
    selection: Selection | None = None,
    resource_version: str | None = None,
    connection: ConnectionOptions | None = None,
) -> int:
    """Fetch the catalog and print per-type totals without downloading.

    Assets are never fetched.  The catalog is read from the cache when it is
    present; when it has to be downloaded the result is **not** written back,
    so this command cannot disturb an existing cache directory.

    Returns:
        ``0`` on success, ``4`` when the remote version cannot be resolved and
        ``5`` when the catalog cannot be fetched or parsed.
    """
    from pathlib import Path

    conn = connection if connection is not None else ConnectionOptions()
    remote = _resolve_remote(
        profile,
        console,
        resource_version=resource_version,
        connection=conn,
    )
    if remote is None:
        return EXIT_VERSION
    if isinstance(remote, _InvalidInput):
        return EXIT_USAGE

    try:
        with HttpClient(**conn.client_kwargs()) as client:  # type: ignore[arg-type]
            assets, _assetinfo = discover_catalog_tree(
                client,
                remote,
                game_version=profile.game_version,
                output=Path(output),
                writable=False,
            )
    except KeyboardInterrupt:
        console.error("interrupted")
        return EXIT_INTERRUPTED
    except Exception as exc:
        return _catalog_error(console, exc)

    active = selection if selection is not None else Selection()
    stats = _build_type_stats(assets, active)

    console.result(f"remote resource version: {remote}")
    console.result(f"{'type':<24}{'count':>8}{'bytes':>16}{'selected':>10}")
    total_count = 0
    total_bytes = 0
    for stat in stats:
        total_count += stat.count
        total_bytes += stat.bytes
        console.result(
            f"{stat.type_name:<24}{stat.count:>8}{stat.bytes:>16}"
            f"{stat.selected_count:>10}"
        )
    console.result(f"{'total':<24}{total_count:>8}{total_bytes:>16}")
    return 0


def _load_previous_files(
    store: SyncStateStore,
    console: Console,
) -> dict[str, dict[str, object]]:
    """Read the prior per-file records, tolerating a missing snapshot.

    A dry run must not fail just because nothing has been synced yet, and a
    damaged snapshot is treated as empty just like the real sync does.

    Args:
        store: Snapshot location for the output directory.
        console: Sink for the diagnostics.

    Returns:
        Previous ``files`` records keyed by relative path; empty when absent.
    """
    if not store.path.exists():
        console.debug("no previous sync state; nothing can be classified as removed")
        return {}
    try:
        state = store.load()
    except (OSError, ValueError) as exc:
        console.warning(f"cannot read the previous sync state: {exc}")
        return {}
    files = state.get("files", {})
    if not isinstance(files, dict):
        console.warning("ignoring an invalid files field in the sync state")
        return {}
    return {str(path): info for path, info in files.items() if isinstance(info, dict)}


def _format_bytes(value: int) -> str:
    """Render a byte count with a binary unit suffix."""
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def run_dry_run(
    output: str,
    profile: VersionProfile,
    console: Console,
    *,
    selection: Selection | None = None,
    force: bool = False,
    verify: bool = False,
    resource_version: str | None = None,
    connection: ConnectionOptions | None = None,
) -> int:
    """Report what a sync would add, change or remove without touching disk.

    The catalog is fetched with ``writable=False`` and classified through
    :meth:`~prism_sync.downloader.AssetDownloader.classify_existing`, which only
    reads.  No asset is downloaded, no state snapshot is written and the output
    directory is never created.

    Returns:
        ``0`` on success (regardless of what the plan contains), ``4`` when the
        remote version cannot be resolved, ``2`` for a malformed explicit
        version and ``5`` when the catalog cannot be read.
    """
    conn = connection if connection is not None else ConnectionOptions()
    remote = _resolve_remote(
        profile,
        console,
        resource_version=resource_version,
        connection=conn,
    )
    if remote is None:
        return EXIT_VERSION
    if isinstance(remote, _InvalidInput):
        return EXIT_USAGE

    target = Path(output)
    store = SyncStateStore(target / DEFAULT_STATE_FILE)
    previous = _load_previous_files(store, console)

    try:
        with HttpClient(**conn.client_kwargs()) as client:  # type: ignore[arg-type]
            assets, _assetinfo = discover_catalog_tree(
                client,
                remote,
                game_version=profile.game_version,
                output=target,
                writable=False,
            )
            downloader = AssetDownloader(
                client,
                force=force,
                verify=verify,
                previous_entries=previous,
            )
            plan = build_sync_plan(
                assets,
                output=target,
                selection=selection,
                previous_entries=previous,
                downloader=downloader,
            )
    except KeyboardInterrupt:
        console.error("interrupted")
        return EXIT_INTERRUPTED
    except Exception as exc:
        return _catalog_error(console, exc)

    console.result(f"remote resource version: {remote}")
    console.result(f"output: {target} (dry run, nothing is written)")
    console.result(f"catalog assets: {plan.total_count}")
    if plan.selection:
        console.result(f"selection: {plan.selection}")
    console.result(f"selected: {plan.selected_count}")
    console.result(f"added: {plan.added_count}")
    console.result(f"changed: {plan.changed_count}")
    console.result(f"unchanged: {plan.unchanged_count}")
    console.debug(
        "classification uses file size; --deep adds SHA-256, "
        "--verify re-downloads assets whose size cannot be derived"
    )
    console.result(
        f"removed: {plan.removed_count} "
        "(previously tracked in sync state, no longer in the catalog; "
        "nothing is deleted)"
    )
    console.result(f"downloads: {plan.added_count + plan.changed_count}")
    console.result(f"estimated download: {_format_bytes(plan.required_bytes)}")
    if plan.free_bytes is None:
        console.result("free space: unavailable")
        console.result("enough space: unknown")
    else:
        console.result(f"free space: {_format_bytes(plan.free_bytes)}")
        verdict = "yes" if plan.enough_space else "no"
        console.result(f"enough space: {verdict}")
    return 0


def _extractor_argv(args: argparse.Namespace) -> list[str] | None:
    """Return the extractor command line, or ``None`` when none was given.

    Args:
        args: Parsed ``profile import-device`` arguments.

    Returns:
        The argument vector, or ``None`` when the caller supplied neither
        ``--extractor`` nor ``--extractor-project``.
    """
    if args.extractor:
        return list(args.extractor)
    if args.extractor_project:
        return [
            "uv",
            "run",
            "--project",
            str(args.extractor_project),
            "prism-version-hash",
        ]
    return None


def _confirm(prompt: str) -> bool:
    """Ask the user a yes/no question on the terminal.

    Args:
        prompt: Question shown to the user.

    Returns:
        ``True`` when the answer starts with ``y`` (case-insensitive).
    """
    try:
        answer = input(f"{prompt} [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower().startswith("y")


def run_profile_import(
    console: Console,
    *,
    profile_path: str,
    name: str | None,
    extractor: list[str] | None,
    timeout: float,
    replace: bool,
    assume_yes: bool,
    check_remote: bool,
) -> int:
    """Extract a version hash from a device and store it in the profile file.

    The hash comes from an external extractor; this function only validates
    what it receives.  Nothing is downloaded and no sync state is touched.

    Args:
        console: Sink for all user-facing output.
        profile_path: Profile document to create or update.
        name: Profile name to write; defaults to the game version.
        extractor: Extractor argument vector, excluding ``--json``.
        timeout: Seconds to wait for the extractor.
        replace: Overwrite an existing profile with the same name.
        assume_yes: Skip the confirmation prompt.
        check_remote: Also resolve the resource version over gRPC.

    Returns:
        ``0`` on success, ``2`` for bad input, ``3`` for profile problems and
        ``4`` when the extractor fails or the remote lookup is rejected.
    """
    if extractor is None:
        console.error(
            "no extractor given: pass --extractor-project DIR or "
            "--extractor CMD (repeat the flag once per argument)"
        )
        return EXIT_USAGE

    console.status(f"running the extractor {extractor[0]} ...")
    try:
        result = extract_android_version_hash(extractor, timeout=timeout)
    except ExtractorUnavailable as exc:
        console.error(f"extractor unavailable: {exc}")
        return EXIT_VERSION
    except ExtractorInconclusive as exc:
        console.error(f"extractor result rejected: {exc}")
        return EXIT_VERSION

    if not result.game_version:
        console.error(
            "the extractor did not report a game version, which is required "
            "to name and validate the profile; check that the device is "
            "reachable over adb"
        )
        return EXIT_VERSION

    game_version = result.game_version
    profile_name = name or game_version
    profile = VersionProfile(
        game_version=game_version,
        version_hash=result.version_hash,
    )

    console.result(f"device: {result.device or 'unknown'}")
    console.result(f"game version: {game_version}")
    console.result(f"version hash: {result.version_hash}")
    console.result(f"profile name: {profile_name}")

    existing = _load_existing_profiles(profile_path, console)
    if existing is None:
        return EXIT_PROFILE

    if profile_name in existing and not replace:
        console.error(
            f"profile {profile_name!r} already exists in {profile_path}; "
            "pass --replace to overwrite it"
        )
        return EXIT_PROFILE

    same_hash = [
        other
        for other, known in existing.items()
        if known.version_hash == result.version_hash and other != profile_name
    ]
    if same_hash:
        console.warning(
            f"the same hash is already stored as profile(s): "
            f"{', '.join(sorted(same_hash))}"
        )

    if not assume_yes:
        action = "overwrite" if profile_name in existing else "write"
        if not _confirm(f"{action} profile {profile_name!r} in {profile_path}?"):
            console.warning("aborted; the profile document was not changed")
            return EXIT_USAGE

    updated = dict(existing)
    updated[profile_name] = profile
    # Keep the document's existing default unless this is the first profile.
    current_default = _default_of(profile_path) if existing else ""
    default_name = current_default or profile_name
    try:
        save_profiles(profile_path, updated, default_name)
    except (OSError, ValueError, VersionError) as exc:
        console.error(f"cannot write {profile_path}: {exc}")
        return EXIT_PROFILE
    console.result(f"wrote profile {profile_name!r} to {profile_path}")

    if check_remote:
        console.status("querying the remote version ...")
        try:
            remote = resolve_remote_version(profile, timeout=15.0, attempts=1)
        except (VersionError, ValueError) as exc:
            console.error(f"remote lookup failed: {exc}")
            console.warning(
                f"profile {profile_name!r} was already saved to {profile_path}; "
                "the remote lookup failure does not affect it"
            )
            return EXIT_VERSION
        console.result(f"resource version: {remote}")

    return 0


def _load_existing_profiles(
    profile_path: str,
    console: Console,
) -> dict[str, VersionProfile] | None:
    """Read the profile document, treating a missing file as empty.

    Args:
        profile_path: Profile document to read.
        console: Sink for diagnostics.

    Returns:
        The stored profiles, or ``None`` when the document is unreadable.
    """
    path = Path(profile_path)
    if not path.exists():
        return {}
    try:
        profiles, _default = load_profiles(path)
    except (OSError, ValueError, VersionError) as exc:
        console.error(f"cannot read {profile_path}: {exc}")
        return None
    return dict(profiles)


def _default_of(profile_path: str) -> str:
    """Return the document's current default profile name, or an empty string.

    Args:
        profile_path: Profile document to read.

    Returns:
        The stored default name, or ``""`` when it cannot be read.
    """
    try:
        _profiles, default_name = load_profiles(profile_path)
    except (OSError, ValueError, VersionError):
        return ""
    return default_name


def run_sync(
    output: str,
    profile: VersionProfile,
    console: Console,
    *,
    selection: Selection | None = None,
    jobs: int | None = None,
    force: bool = False,
    verify: bool = False,
    resource_version: str | None = None,
    connection: ConnectionOptions | None = None,
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
        resource_version: Use this version directly and skip the gRPC lookup.
        connection: HTTP and gRPC overrides; ``None`` keeps the defaults.

    Returns:
        Process exit status: ``0`` when up to date or fully synced, ``4`` when
        the remote version cannot be resolved, ``5`` when the sync itself
        fails, ``6`` when some assets failed and ``130`` when interrupted.
    """
    console.debug(f"game version {profile.game_version!r}")
    console.debug(f"gRPC target {profile.target}")

    conn = connection if connection is not None else ConnectionOptions()
    if resource_version is not None:
        console.debug(f"using the supplied resource version {resource_version!r}")
    try:
        remote = resolve_remote_version(
            profile,
            resource_version=resource_version,
            **conn.version_kwargs(),  # type: ignore[arg-type]
        )
    except VersionError as exc:
        console.error(f"cannot resolve the remote version: {exc}")
        return EXIT_VERSION
    except ValueError as exc:
        console.error(f"invalid resource version: {exc}")
        return EXIT_USAGE
    console.debug(f"remote resource version {remote}")
    if selection is not None and selection.active:
        console.debug(f"selection {selection.to_dict()}")
    console.debug(f"jobs {jobs if jobs is not None else 'default'}")

    outcome: SyncOutcome | None = None
    try:
        with HttpClient(**conn.client_kwargs()) as client:  # type: ignore[arg-type]
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

    if args.command == "profile":
        if args.profile_command != "import-device":
            parser.print_help(sys.stderr)
            return EXIT_USAGE
        console = Console(command="profile import-device")
        return run_profile_import(
            console,
            profile_path=args.profile,
            name=args.name,
            extractor=_extractor_argv(args),
            timeout=args.timeout,
            replace=args.replace,
            assume_yes=args.yes,
            check_remote=args.check_remote,
        )

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

        connection = ConnectionOptions(
            base_url=args.base_url,
            connect_timeout=args.connect_timeout,
            read_timeout=args.read_timeout,
            retries=args.retries,
            no_proxy=args.no_proxy,
            grpc_timeout=args.grpc_timeout,
            grpc_attempts=args.grpc_attempts,
        )
        if connection_error := validate_connection(connection):
            console.error(connection_error)
            return EXIT_USAGE

        read_only = args.check or args.list_types
        if not selection.active and not args.all and not read_only:
            console.error(
                "no filter given: this would synchronize the entire catalog, "
                "which is very large; pass --types/--max-size/--include/"
                "--exclude to narrow it down, or --all to confirm"
            )
            return EXIT_USAGE

        if args.resource_version is not None:
            profile = _profile_for_explicit_version(args, console)
            if profile is None:
                return EXIT_PROFILE
        else:
            try:
                profile = select_profile(args.profile, args.profile_name)
            except ProfileSelectionError as exc:
                console.error(str(exc))
                return EXIT_PROFILE

        if args.check and args.list_types:
            console.error("--check and --list-types cannot be combined")
            return EXIT_USAGE

        if args.dry_run and (args.check or args.list_types):
            other = "--check" if args.check else "--list-types"
            console.error(f"--dry-run cannot be combined with {other}")
            return EXIT_USAGE

        if args.check or args.list_types:
            problem = _read_only_conflict(args)
            if problem is not None:
                console.error(problem)
                return EXIT_USAGE

        if args.dry_run:
            return run_dry_run(
                args.output,
                profile,
                console,
                selection=selection,
                force=args.force,
                verify=args.verify,
                resource_version=args.resource_version,
                connection=connection,
            )

        if args.check:
            return run_check(
                args.output,
                profile,
                console,
                selection=selection,
                resource_version=args.resource_version,
                connection=connection,
            )

        if args.list_types:
            return run_list_types(
                args.output,
                profile,
                console,
                selection=selection,
                resource_version=args.resource_version,
                connection=connection,
            )

        return run_sync(
            args.output,
            profile,
            console,
            selection=selection,
            jobs=args.jobs,
            force=args.force,
            verify=args.verify,
            resource_version=args.resource_version,
            connection=connection,
        )

    parser.print_help(sys.stderr)  # pragma: no cover - argparse rejects these
    return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover - manual invocation
    raise SystemExit(main())
