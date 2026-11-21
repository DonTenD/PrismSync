"""Sync state snapshot storage.

The snapshot is a plain JSON object written to a caller-chosen path.  It has no
schema version and no migration logic: unknown or missing fields are simply
passed through, so snapshots produced by older builds stay readable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._fsutil import write_json_atomic


class SyncStateStore:
    """Read and write the sync state snapshot document.

    The document is a plain ``dict``; this class deliberately does not impose a
    data model so that snapshots from other tools remain readable.

    Attributes:
        path: Location of the snapshot file.
    """

    def __init__(self, path: str | Path) -> None:
        """Bind the store to ``path``.

        Args:
            path: Snapshot file location.  The parent directory is created on
                save; no default location is assumed.
        """
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        """Load the snapshot document.

        Returns:
            The parsed JSON object, or an empty mapping when the file is absent.

        Raises:
            ValueError: If the JSON root is not an object.
            json.JSONDecodeError: If the file is not valid JSON.
            OSError: If the file exists but cannot be read.
        """
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f"state file is not a JSON object: {self.path}")
        return value

    def save(self, state: dict[str, Any]) -> None:
        """Write the snapshot document atomically.

        Args:
            state: JSON-serializable mapping to persist verbatim.

        Raises:
            OSError: If the file cannot be written or replaced.
        """
        write_json_atomic(self.path, state)
