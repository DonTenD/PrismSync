"""Call external host tools and validate their output.

The Android client keeps its ``versionHash`` inside an encrypted metadata blob,
so it can only be read from a running process.  That extraction needs root, a
Frida server and a live game, which is deliberately **not** part of this
library.  This module only shells out to an external extractor and refuses to
trust its answer unless every safety criterion holds.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

#: The extractor must report this confidence for its answer to be usable.
_REQUIRED_CONFIDENCE = "high"

#: A resource version hash is exactly 40 lower-case hex characters.
_HASH_PATTERN = re.compile(r"^[0-9a-f]{40}$")

#: UTF-8 byte order mark, which json.loads refuses to accept.
BOM = bytes((0xEF, 0xBB, 0xBF))

#: Longest stderr excerpt carried into an exception message.
_STDERR_EXCERPT = 500


class ExtractorUnavailable(RuntimeError):
    """The extractor could not be run, timed out, or exited with an error."""


class ExtractorInconclusive(RuntimeError):
    """The extractor ran but its output did not satisfy the safety criteria."""


@dataclass(frozen=True, slots=True)
class AndroidHashResult:
    """A verified ``versionHash`` read from an Android device.

    Attributes:
        version_hash: The verified 40-character hash.
        game_version: Installed ``versionName``, or ``None`` when unavailable.
        device: Device description reported by the extractor, or ``None``.
    """

    version_hash: str
    game_version: str | None
    device: str | None


def _decode(raw: bytes | None) -> str:
    """Decode captured subprocess output.

    The Windows console code page cannot represent Android's UTF-8 output, and
    a strict decode would mask the real failure, so decoding falls back to a
    lossy pass instead of raising.

    Args:
        raw: Captured bytes, possibly ``None``.

    Returns:
        The decoded text; empty when nothing was captured.
    """
    if not raw:
        return ""
    # Strip a leading UTF-8 BOM first: json.loads rejects it, and a Windows tool
    # launched with a BOM-emitting writer would otherwise look like corrupt JSON.
    if raw.startswith(BOM):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _excerpt(text: str, limit: int = _STDERR_EXCERPT) -> str:
    """Return a single-line, length-limited excerpt of ``text``."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "..."


def _require_dict(value: object, where: str) -> dict[str, Any]:
    """Return ``value`` as a dict or raise :class:`ExtractorInconclusive`."""
    if not isinstance(value, dict):
        raise ExtractorInconclusive(f"{where} is not a JSON object")
    return value


def _optional_str(payload: dict[str, Any], key: str) -> str | None:
    """Return ``payload[key]`` when it is a non-empty string, else ``None``."""
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _validate(payload: object) -> AndroidHashResult:
    """Apply every safety criterion to the extractor payload.

    Args:
        payload: Decoded JSON emitted by the extractor.

    Returns:
        The verified result.

    Raises:
        ExtractorInconclusive: If any criterion fails.
    """
    root = _require_dict(payload, "payload")

    version_hash = root.get("version_hash")
    if not isinstance(version_hash, str):
        raise ExtractorInconclusive("version_hash is missing or not a string")
    if not _HASH_PATTERN.match(version_hash):
        raise ExtractorInconclusive(
            f"version_hash is not 40 lower-case hex characters: {version_hash!r}"
        )

    confidence = root.get("confidence")
    if confidence != _REQUIRED_CONFIDENCE:
        raise ExtractorInconclusive(
            f"confidence is {confidence!r}, expected {_REQUIRED_CONFIDENCE!r}"
        )

    candidates = root.get("candidates")
    if not isinstance(candidates, list):
        raise ExtractorInconclusive("candidates is missing or not a list")
    if len(candidates) != 1:
        raise ExtractorInconclusive(
            f"expected exactly 1 candidate, got {len(candidates)}"
        )

    candidate = _require_dict(candidates[0], "candidates[0]")
    if candidate.get("value") != version_hash:
        raise ExtractorInconclusive("candidates[0].value does not match version_hash")
    if candidate.get("format_valid") is not True:
        raise ExtractorInconclusive("candidates[0].format_valid is not true")
    if candidate.get("nul_terminated") is not True:
        raise ExtractorInconclusive("candidates[0].nul_terminated is not true")

    warnings = root.get("warnings")
    if not isinstance(warnings, list):
        raise ExtractorInconclusive("warnings is missing or not a list")
    if warnings:
        raise ExtractorInconclusive(
            f"extractor reported warnings: {_excerpt('; '.join(map(str, warnings)))}"
        )

    return AndroidHashResult(
        version_hash=version_hash,
        game_version=_optional_str(root, "game_version"),
        device=_optional_str(root, "device"),
    )


def extract_android_version_hash(
    command: Sequence[str],
    *,
    timeout: float = 300.0,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
) -> AndroidHashResult:
    """Run an external Android hash extractor and verify its answer.

    The command is expected to accept a trailing ``--json`` flag and print a
    single JSON object.  Extraction needs a rooted device, a running Frida
    server and a loaded game; none of that is managed here.

    Args:
        command: Argument vector for the extractor, without ``--json``.
        timeout: Seconds to wait for the extractor.
        runner: Injection point for tests; defaults to :func:`subprocess.run`.

    Returns:
        The verified hash together with the optional device description.

    Raises:
        ExtractorUnavailable: The command could not start, timed out, or
            exited with a non-zero status.
        ExtractorInconclusive: The output was missing, malformed, or did not
            satisfy the safety criteria.
    """
    if not command:
        raise ExtractorUnavailable("command must not be empty")

    argv = [*command, "--json"]
    execute = runner or subprocess.run

    try:
        completed = execute(
            argv,
            capture_output=True,
            text=False,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExtractorUnavailable(
            f"extractor timed out after {timeout:g}s: {command[0]}"
        ) from exc
    except FileNotFoundError as exc:
        raise ExtractorUnavailable(
            f"extractor command not found: {command[0]}"
        ) from exc
    except OSError as exc:
        raise ExtractorUnavailable(
            f"cannot start extractor {command[0]}: {exc}"
        ) from exc

    stdout = _decode(completed.stdout)
    stderr = _decode(completed.stderr)

    if completed.returncode != 0:
        detail = _excerpt(stderr or stdout) or "no diagnostic output"
        raise ExtractorUnavailable(
            f"extractor exited with status {completed.returncode}: {detail}"
        )

    if not stdout.strip():
        raise ExtractorInconclusive("extractor produced no output on stdout")

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ExtractorInconclusive(
            f"extractor output is not valid JSON: {exc.msg}"
        ) from exc

    return _validate(payload)
