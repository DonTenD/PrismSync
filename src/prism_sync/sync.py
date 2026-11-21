"""Minimal catalog synchronization orchestration.

This module wires the already-migrated pieces together: catalog discovery,
asset selection, concurrent downloads and the state snapshot.  It deliberately
stays small - version checking, the version marker, pruning, localization and
MasterData extraction live outside the library core.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .catalog_client import catalog_fingerprint, discover_catalog_tree
from .downloader import AssetDownloader, DownloadResult, download_assets
from .errors import DownloadCancelled
from .http_client import HttpClient
from .models import CatalogAsset, SyncOutcome, SyncResult, VersionProfile
from .selection import Selection
from .state import SyncStateStore
from .version import check_version, write_local_version

LOGGER = logging.getLogger(__name__)

DEFAULT_STATE_FILE = "sync-state.json"


def _load_previous_entries(store: SyncStateStore) -> dict[str, dict[str, Any]]:
    """Read prior per-file records, tolerating a corrupt or partial snapshot.

    Matches the original behaviour: an unreadable state file is logged and
    treated as empty so a damaged snapshot never blocks a sync.
    """
    try:
        state = store.load()
    except (OSError, ValueError) as exc:
        LOGGER.warning("cannot read previous sync state %s: %s", store.path, exc)
        return {}

    files = state.get("files", {})
    if not isinstance(files, dict):
        LOGGER.warning("ignoring invalid files field in %s", store.path)
        return {}
    return {str(path): info for path, info in files.items() if isinstance(info, dict)}


def _result_record(asset: CatalogAsset, outcome: Any) -> dict[str, Any]:
    """Build the ``files`` entry for one processed asset."""
    if isinstance(outcome, DownloadResult):
        return {
            "size": outcome.size,
            "rawSize": outcome.raw_size,
            "realName": asset.real_name,
            "status": outcome.status,
            "verified": outcome.verified,
            "rawMd5": outcome.raw_md5,
            "sha256": outcome.sha256,
            "etag": outcome.etag,
        }
    return {
        "size": None,
        "realName": asset.real_name,
        "status": "failed",
        "error": str(outcome),
    }


def _snapshot_files(
    records: dict[str, dict[str, Any]],
    previous_entries: dict[str, dict[str, Any]],
    catalog_paths: set[str],
) -> dict[str, dict[str, Any]]:
    """Merge this run's records with prior ones still present in the catalog.

    Mirrors ``_state_files_for_snapshot``: entries handled by this run win, and
    untouched entries survive only while their path is still part of the
    catalog, so files that disappeared from the catalog are dropped.
    """
    files = dict(records)
    for path, info in previous_entries.items():
        if path not in files and path in catalog_paths:
            files[path] = info
    return files


def sync_resources(
    client: HttpClient,
    version: str,
    *,
    game_version: str,
    output: Path,
    jobs: int = 8,
    force: bool = False,
    verify: bool = False,
    deep: bool = False,
    resume: bool = True,
    use_cache: bool = True,
    refresh: bool = False,
    writable: bool = True,
    selection: Selection | None = None,
    state_path: Path | None = None,
    stop_event: threading.Event | None = None,
    on_progress: Callable[[CatalogAsset, int, int], None] | None = None,
) -> SyncResult:
    """Discover, download and record the resources for ``version``.

    The caller owns ``client``; this function never closes it.  Version checking
    and the ``.prism-version.json`` marker are intentionally left to the caller
    so a plain library user can decide when a version is considered complete.

    A state snapshot is written only when the batch ran to completion.  If any
    result is ``DownloadCancelled`` or ``None`` (the batch was cancelled), or an
    exception escapes, nothing is written.

    Args:
        client: HTTP client used for catalog and asset requests.
        version: ``"<simple>@<base64>"`` resource version.
        game_version: Game version used to derive the root catalog CRC.
        output: Output root; resolved once and used for every path check.
        jobs: Maximum concurrent downloads, ``1 <= jobs <= MAX_JOBS``.
        force: Re-download even when the local file looks valid.
        verify: Re-download when the transformed size is not derivable.
        deep: Re-hash existing files to confirm content identity.
        resume: Resume interrupted ``.part.raw`` transfers.
        use_cache: Read catalogs from ``<output>/.catalog-cache``.
        refresh: Revalidate cached catalogs with the CDN.
        writable: Allow writing the catalog cache.
        selection: Optional asset filter; ``None`` keeps every asset.
        state_path: Snapshot location; defaults to ``output/sync-state.json``.
        stop_event: Shared cancellation event.
        on_progress: Called from the scheduling thread after each asset.

    Returns:
        A :class:`~prism_sync.models.SyncResult` whose ``results`` align with
        ``assets``.

    Raises:
        CatalogTreeError: On a catalog tree problem.
        ValueError: If ``version`` or ``jobs`` is invalid.
        KeyboardInterrupt: If the caller interrupts; no snapshot is written.
        OSError: If the state snapshot cannot be written.
        BaseException: Any ``on_progress`` failure, with no snapshot written.
    """
    assets, assetinfo = discover_catalog_tree(
        client,
        version,
        game_version=game_version,
        output=output,
        use_cache=use_cache,
        refresh=refresh,
        writable=writable,
    )
    fingerprint = catalog_fingerprint(assetinfo)

    catalog_paths = {asset.relative_path for asset in assets}
    selected = (
        assets
        if selection is None
        else [asset for asset in assets if selection.matches(asset)]
    )

    snapshot_path = (
        state_path if state_path is not None else output / DEFAULT_STATE_FILE
    )
    store = SyncStateStore(snapshot_path)
    previous_entries = _load_previous_entries(store)

    downloader = AssetDownloader(
        client,
        force=force,
        verify=verify,
        deep=deep,
        resume=resume,
        previous_entries=previous_entries,
        stop_event=stop_event,
    )
    outcomes = download_assets(
        downloader,
        selected,
        jobs=jobs,
        on_progress=on_progress,
    )

    downloaded = skipped = failed = resumed_count = 0
    records: dict[str, dict[str, Any]] = {}
    cancelled = False
    for asset, outcome in zip(selected, outcomes, strict=True):
        if outcome is None or isinstance(outcome, DownloadCancelled):
            cancelled = True
            continue
        if isinstance(outcome, DownloadResult):
            if outcome.status == "skipped":
                skipped += 1
            else:
                downloaded += 1
                if outcome.resumed:
                    resumed_count += 1
        else:
            failed += 1
        records[asset.relative_path] = _result_record(asset, outcome)

    result = SyncResult(
        assets=tuple(selected),
        results=tuple(outcomes),
        downloaded=downloaded,
        skipped=skipped,
        failed=failed,
        resumed=resumed_count,
        catalog_fingerprint=fingerprint,
        catalog_count=len(assetinfo),
        total_asset_count=len(assets),
        state=None,
    )

    if cancelled:
        return result

    snapshot: dict[str, Any] = {
        "checkedAt": datetime.now(UTC).isoformat(),
        "status": "OK" if failed == 0 else "PARTIAL",
        "gameVersion": game_version,
        "resourceVersion": version,
        "catalogFingerprint": fingerprint,
        "summary": {
            "catalogs": len(assetinfo),
            "assets": len(selected),
            "totalAssets": len(assets),
            "downloaded": downloaded,
            "skipped": skipped,
            "failed": failed,
            "resumed": resumed_count,
            "pruned": 0,
            "localizetextCategories": None,
        },
        "selection": (
            selection.to_dict() if selection is not None and selection.active else None
        ),
        "files": _snapshot_files(records, previous_entries, catalog_paths),
    }
    store.save(snapshot)
    result.state = snapshot
    return result


def sync_if_needed(
    client: HttpClient,
    version: str,
    *,
    game_version: str,
    output: Path,
    force: bool = False,
    verify: bool = False,
    deep: bool = False,
    resume: bool = True,
    jobs: int = 8,
    use_cache: bool = True,
    refresh: bool = False,
    writable: bool = True,
    selection: Selection | None = None,
    state_path: Path | None = None,
    stop_event: threading.Event | None = None,
    on_progress: Callable[[CatalogAsset, int, int], None] | None = None,
) -> SyncOutcome:
    """Synchronize ``version`` unless the local copy is already current.

    Combines :func:`~prism_sync.version.check_version`,
    :func:`sync_resources` and :func:`~prism_sync.version.write_local_version`.
    The remote version is never fetched here: pass an already resolved
    ``version`` so this stays offline-friendly and free of gRPC.

    The local marker is written only when the sync ran to completion, produced
    no failures, left no cancelled or unstarted assets and saved its state
    snapshot.  A partial or cancelled run returns the real result with
    ``marker_written=False`` so a later run retries it.

    Args:
        client: HTTP client used for catalog and asset requests.
        version: ``"<simple>@<base64>"`` resource version.
        game_version: Game version used for the version comparison and the root
            catalog CRC.
        output: Output root.
        force: Re-download even when the local files look valid.  Also forces a
            sync when the version comparison says the copy is current.
        verify: Re-download when the transformed size is not derivable.  Also
            forces a sync when the copy is current.
        deep: Re-hash existing files to confirm content identity.  Also forces a
            sync when the copy is current.
        resume: Resume interrupted transfers.
        jobs: Maximum concurrent downloads.
        use_cache: Read catalogs from the local cache.
        refresh: Revalidate cached catalogs with the CDN.
        writable: Allow writing the catalog cache.
        selection: Optional asset filter.
        state_path: Snapshot location; defaults to ``output/sync-state.json``.
        stop_event: Shared cancellation event.
        on_progress: Called from the scheduling thread after each asset.

    Returns:
        A :class:`~prism_sync.models.SyncOutcome` describing the decision, the
        sync result (``None`` when skipped) and whether the marker was written.

    Raises:
        VersionError: If ``version`` cannot be decoded for the comparison.
        CatalogTreeError: On a catalog tree problem.
        ValueError: If ``jobs`` is invalid.
        KeyboardInterrupt: If the caller interrupts; no marker is written.
        OSError: If the state snapshot or the version marker cannot be written.
        BaseException: Any ``on_progress`` failure, with no marker written.
    """
    # ``check_version`` only reads ``game_version``; the hash is unused here and
    # ``game_version`` must match the one used for the root catalog CRC.
    check = check_version(
        VersionProfile(game_version=game_version, version_hash=""),
        output,
        version,
        selection,
    )

    if check.up_to_date and not (force or verify or deep):
        return SyncOutcome(check=check, result=None, marker_written=False)

    result = sync_resources(
        client,
        version,
        game_version=game_version,
        output=output,
        jobs=jobs,
        force=force,
        verify=verify,
        deep=deep,
        resume=resume,
        use_cache=use_cache,
        refresh=refresh,
        writable=writable,
        selection=selection,
        state_path=state_path,
        stop_event=stop_event,
        on_progress=on_progress,
    )

    # A snapshot is only written for a complete batch, and ``failed`` alone is
    # not enough: cancelled or unstarted assets and any recorded exception all
    # mean the tree on disk is not fully verified.
    incomplete = (
        result.state is None
        or result.failed > 0
        or any(
            item is None or isinstance(item, (Exception, DownloadCancelled))
            for item in result.results
        )
    )
    if incomplete:
        return SyncOutcome(check=check, result=result, marker_written=False)

    marker_selection = (
        selection.to_dict() if selection is not None and selection.active else None
    )
    if (
        marker_selection is not None
        and check.local is not None
        and check.local.selection is None
        and check.local.game_version == game_version
        and check.local.resource_version == check.remote_resource_version
    ):
        # The previous marker already describes a full sync of this version, so
        # a subset sync keeps it instead of narrowing the marker.
        marker_selection = None

    write_local_version(
        output,
        game_version=game_version,
        resource_version=version,
        catalog_fingerprint=result.catalog_fingerprint,
        selection=marker_selection,
    )
    return SyncOutcome(check=check, result=result, marker_written=True)
