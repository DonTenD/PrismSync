"""Internal filesystem helpers shared by cache and download metadata writers."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, value: Any) -> None:
    """Write JSON through a temporary file and ``os.replace``.

    The temporary file is created in the target's own directory so the final
    replace stays on one filesystem.  The previous file is never truncated
    before the new content is fully written.

    Args:
        path: Destination file.
        value: JSON-serializable value.

    Raises:
        OSError: If the temporary file cannot be written or replaced.
    """
    target = path
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            handle.write("\n")
        Path(tmp_name).replace(target)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp_name).unlink()
        raise
