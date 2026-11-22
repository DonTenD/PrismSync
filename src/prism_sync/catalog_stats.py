"""Per-type catalog statistics.

Only aggregates already-discovered assets; nothing here touches the network or
the filesystem.
"""

from __future__ import annotations

from collections.abc import Iterable

from .models import CatalogAsset
from .selection import Selection, TypeStat


def _build_type_stats(
    assets: Iterable[CatalogAsset],
    selection: Selection,
) -> list[TypeStat]:
    """Return per-type totals for ``assets``, split by ``selection``.

    Every asset counts towards ``count``/``bytes``; assets accepted by
    ``selection.matches`` also count towards ``selected_count``/
    ``selected_bytes``.

    Args:
        assets: Discovered catalog assets.
        selection: Filter used to mark the selected subset.

    Returns:
        One :class:`~prism_sync.selection.TypeStat` per type name, sorted by
        type name so the output is stable.
    """
    counts: dict[str, list[int]] = {}
    for asset in assets:
        type_name = asset.entry.type_name
        data = counts.setdefault(type_name, [0, 0, 0, 0])
        data[0] += 1
        data[1] += asset.entry.size
        if selection.matches(asset):
            data[2] += 1
            data[3] += asset.entry.size

    stats = [
        TypeStat(type_name, data[0], data[1], data[2], data[3])
        for type_name, data in counts.items()
    ]
    stats.sort(key=lambda stat: stat.type_name)
    return stats
