"""Asset selection filters and sync planning data.

The catalog contains roughly 190k assets.  A selection narrows a sync to a
subset (for example only ``json``/``bytes`` files, or only names matching a
glob) while ``assetinfo.json`` still records the complete catalog.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any

from .models import CatalogAsset

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?B?)\s*$", re.IGNORECASE)
_SIZE_UNITS: dict[str, int] = {
    "": 1,
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "M": 1024**2,
    "MB": 1024**2,
    "G": 1024**3,
    "GB": 1024**3,
    "T": 1024**4,
    "TB": 1024**4,
}


def parse_size(value: str) -> int:
    """Parse ``1024``, ``10KB``, ``1.5MB`` or ``2 GB`` into bytes."""
    match = _SIZE_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid size: {value!r}")
    number = float(match.group(1))
    unit = match.group(2).upper()
    factor = _SIZE_UNITS.get(unit)
    if factor is None:
        raise ValueError(f"invalid size unit: {unit!r}")
    return int(number * factor)


def parse_types(value: str) -> frozenset[str]:
    """Parse a comma-separated type list (``json,bytes,acb``)."""
    types = frozenset(part.strip().lower() for part in value.split(",") if part.strip())
    if not types:
        raise ValueError("types must contain at least one value")
    return types


def _matches(names: tuple[str, ...], pattern: str) -> bool:
    lowered = pattern.lower()
    return any(fnmatch.fnmatchcase(name.lower(), lowered) for name in names)


@dataclass(frozen=True, slots=True)
class Selection:
    """Filters applied to catalog assets.

    ``include``/``exclude`` patterns are matched against the catalog name
    (``localizetext.bytes``), the relative path
    (``root/raw/localizetext.bytes``) and the bare label (``localizetext``).
    Exclusions win over inclusions.
    """

    types: frozenset[str] | None = None
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_size: int | None = None

    def __post_init__(self) -> None:
        """Normalize and validate selection parameters on initialization."""
        if self.types is not None:
            normalized = frozenset(
                part.strip().lower() for part in self.types if part.strip()
            )
            object.__setattr__(self, "types", normalized)
        object.__setattr__(
            self,
            "include",
            tuple(sorted(pattern.lower() for pattern in self.include if pattern)),
        )
        object.__setattr__(
            self,
            "exclude",
            tuple(sorted(pattern.lower() for pattern in self.exclude if pattern)),
        )
        if self.max_size is not None and self.max_size < 0:
            raise ValueError("max_size must be non-negative")

    @property
    def active(self) -> bool:
        """Return True if any selection filter is active."""
        return bool(
            self.types is not None
            or self.include
            or self.exclude
            or self.max_size is not None
        )

    def matches(self, asset: CatalogAsset) -> bool:
        """Check if the given asset matches the selection criteria."""
        entry = asset.entry
        if self.types is not None and entry.type_name.lower() not in self.types:
            return False
        if self.max_size is not None and entry.size > self.max_size:
            return False

        names = (
            f"{entry.label}.{entry.type_name}",
            asset.relative_path,
            entry.label,
        )
        if self.include and not any(
            _matches(names, pattern) for pattern in self.include
        ):
            return False
        if self.exclude:
            return not any(_matches(names, pattern) for pattern in self.exclude)
        return True

    def to_dict(self) -> dict[str, object]:
        """Convert selection to dictionary for JSON serialization."""
        return {
            "active": self.active,
            "types": sorted(self.types) if self.types is not None else None,
            "include": list(self.include),
            "exclude": list(self.exclude),
            "maxSize": self.max_size,
        }


@dataclass(frozen=True, slots=True)
class TypeStat:
    """Per-type totals for ``--list-types``."""

    type_name: str
    count: int
    bytes: int
    selected_count: int
    selected_bytes: int

    def to_dict(self) -> dict[str, object]:
        """Convert type stat to dictionary for JSON serialization."""
        return {
            "type": self.type_name,
            "count": self.count,
            "bytes": self.bytes,
            "selectedCount": self.selected_count,
            "selectedBytes": self.selected_bytes,
        }


@dataclass(frozen=True, slots=True)
class PlanTypeStat:
    """Per-type breakdown of a dry-run plan."""

    type_name: str
    added: int
    added_bytes: int
    changed: int
    changed_bytes: int
    unchanged: int
    unchanged_bytes: int

    @property
    def download_count(self) -> int:
        """Return the total number of files to download."""
        return self.added + self.changed

    @property
    def download_bytes(self) -> int:
        """Return the total number of bytes to download."""
        return self.added_bytes + self.changed_bytes

    def to_dict(self) -> dict[str, object]:
        """Convert plan type stat to dictionary for JSON serialization."""
        return {
            "type": self.type_name,
            "added": self.added,
            "addedBytes": self.added_bytes,
            "changed": self.changed,
            "changedBytes": self.changed_bytes,
            "unchanged": self.unchanged,
            "unchangedBytes": self.unchanged_bytes,
            "download": self.download_count,
            "downloadBytes": self.download_bytes,
        }


@dataclass(frozen=True, slots=True)
class SyncPlan:
    """Read-only description of what a sync would add, change or remove."""

    selection: dict[str, Any]
    total_count: int
    total_bytes: int
    selected_count: int
    selected_bytes: int
    added_count: int
    added_bytes: int
    changed_count: int
    changed_bytes: int
    unchanged_count: int
    unchanged_bytes: int
    removed_count: int
    removed_bytes: int
    free_bytes: int | None
    required_bytes: int
    enough_space: bool | None
    by_type: list[PlanTypeStat]

    def to_dict(self) -> dict[str, object]:
        """Convert sync plan to dictionary for JSON serialization."""
        return {
            "selection": self.selection,
            "totalCount": self.total_count,
            "totalBytes": self.total_bytes,
            "selectedCount": self.selected_count,
            "selectedBytes": self.selected_bytes,
            "addedCount": self.added_count,
            "addedBytes": self.added_bytes,
            "changedCount": self.changed_count,
            "changedBytes": self.changed_bytes,
            "unchangedCount": self.unchanged_count,
            "unchangedBytes": self.unchanged_bytes,
            "removedCount": self.removed_count,
            "removedBytes": self.removed_bytes,
            "freeBytes": self.free_bytes,
            "requiredBytes": self.required_bytes,
            "enoughSpace": self.enough_space,
            "byType": [t.to_dict() for t in self.by_type],
        }
