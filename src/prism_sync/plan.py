"""Build a read-only sync plan.

The plan classifies every catalog asset against the local filesystem and the
previous sync snapshot.  Nothing in this module downloads, deletes or writes:
the only filesystem access is ``stat``/``read`` for classification and
``shutil.disk_usage`` for the free-space estimate.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .downloader import AssetDownloader
from .models import CatalogAsset
from .selection import PlanTypeStat, Selection, SyncPlan

LOGGER = logging.getLogger(__name__)


def free_bytes_for(path: Path) -> int | None:
    """Return free bytes on the filesystem holding ``path``.

    The nearest existing ancestor is used, so the target directory does not
    have to exist yet and nothing is created to probe the filesystem.

    Args:
        path: Target output directory, which may not exist.

    Returns:
        Free bytes, or ``None`` when they cannot be determined.
    """
    probe = path
    while True:
        try:
            return shutil.disk_usage(probe).free
        except OSError:
            parent = probe.parent
            if parent == probe:
                return None
            probe = parent


def _removed_from_state(
    previous_entries: Mapping[str, Mapping[str, Any]],
    catalog_paths: set[str],
) -> tuple[int, int]:
    """Count state entries whose path is no longer part of the catalog.

    Only paths recorded in the previous snapshot are considered, because that
    is the set of paths this tool tracks.  The output directory is **not**
    scanned: a file that was never recorded by a sync is invisible here, and
    this function must never grow a filesystem walk just to count it.

    A counted entry is not proof that a file exists: ``status`` may be
    ``failed`` (the download never produced a final file) and the entry has no
    ``size``, so it contributes to ``count`` but not to the byte total.  This
    is a reporting number only; nothing is ever deleted.

    ``catalog_paths`` must be the **unfiltered** catalog, or selection would
    look like removal.
    """
    count = 0
    total = 0
    for relative, info in previous_entries.items():
        if relative in catalog_paths:
            continue
        size = info.get("size")
        count += 1
        if isinstance(size, int) and size > 0:
            total += size
    return count, total


def build_sync_plan(
    assets: Sequence[CatalogAsset],
    *,
    output: Path,
    selection: Selection | None = None,
    previous_entries: Mapping[str, Mapping[str, Any]] | None = None,
    downloader: AssetDownloader,
) -> SyncPlan:
    """Classify ``assets`` into added/changed/unchanged and count removals.

    ``assets`` must be the **complete, unfiltered** catalog: selection only
    decides which remote assets enter the download plan, and removals are
    derived from the full catalog so unselected assets are never reported as
    removed.

    Args:
        assets: Every asset in the catalog, before filtering.
        output: Output root, used only for the free-space estimate.
        selection: Filter deciding which assets would be downloaded.
        previous_entries: ``files`` mapping from the previous sync snapshot.
        downloader: Used for :meth:`AssetDownloader.classify_existing`.

    Returns:
        A populated :class:`~prism_sync.selection.SyncPlan`.
    """
    active = selection if selection is not None else Selection()
    prior = previous_entries if previous_entries is not None else {}

    total_count = 0
    total_bytes = 0
    selected_count = 0
    selected_bytes = 0
    added_count = added_bytes = 0
    changed_count = changed_bytes = 0
    unchanged_count = unchanged_bytes = 0
    per_type: dict[str, list[int]] = {}

    for asset in assets:
        size = asset.entry.size
        total_count += 1
        total_bytes += size
        if not active.matches(asset):
            continue

        selected_count += 1
        selected_bytes += size
        kind = downloader.classify_existing(asset)

        bucket = per_type.setdefault(asset.entry.type_name, [0, 0, 0, 0, 0, 0])
        if kind == "added":
            added_count += 1
            added_bytes += size
            bucket[0] += 1
            bucket[1] += size
        elif kind == "changed":
            changed_count += 1
            changed_bytes += size
            bucket[2] += 1
            bucket[3] += size
        else:
            unchanged_count += 1
            unchanged_bytes += size
            bucket[4] += 1
            bucket[5] += size

    removed_count, removed_bytes = _removed_from_state(
        prior, {asset.relative_path for asset in assets}
    )

    required_bytes = added_bytes + changed_bytes
    free_bytes = free_bytes_for(output)
    enough_space = None if free_bytes is None else free_bytes >= required_bytes

    by_type = [PlanTypeStat(name, *values) for name, values in sorted(per_type.items())]

    return SyncPlan(
        selection=active.to_dict() if active.active else {},
        total_count=total_count,
        total_bytes=total_bytes,
        selected_count=selected_count,
        selected_bytes=selected_bytes,
        added_count=added_count,
        added_bytes=added_bytes,
        changed_count=changed_count,
        changed_bytes=changed_bytes,
        unchanged_count=unchanged_count,
        unchanged_bytes=unchanged_bytes,
        removed_count=removed_count,
        removed_bytes=removed_bytes,
        free_bytes=free_bytes,
        required_bytes=required_bytes,
        enough_space=enough_space,
        by_type=by_type,
    )
