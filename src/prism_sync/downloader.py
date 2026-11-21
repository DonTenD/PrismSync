"""Streaming asset downloader with resume and content verification.

The CDN is Google Cloud Storage: it supports ``Range`` requests and returns
``x-goog-hash: md5=...`` (plus an MD5 ``ETag``).  Downloads are staged as
``.part.raw`` + ``.part.json`` so an interrupted transfer can resume, then
transformed into ``.part.tmp`` and atomically renamed into place.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol

import httpx
import lz4.frame
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ._fsutil import write_json_atomic
from .errors import DownloadCancelled
from .http_client import (
    DownloadError,
    HttpClient,
    NotFoundError,
    RangeNotSatisfiableError,
    RetryableHTTPError,
    build_asset_url,
)
from .models import (
    MAX_JOBS,
    CatalogAsset,
    CatalogEntry,
    ResourceType,
    UnsupportedResourceType,
)

LOGGER = logging.getLogger(__name__)

PART_RAW_SUFFIX = ".part.raw"
PART_META_SUFFIX = ".part.json"
PART_TMP_SUFFIX = ".part.tmp"

#: The four-byte prefix on ``BundlePaddedStart`` objects (``UnityFS`` follows).
PADDED_BUNDLE_PREFIX = bytes.fromhex("BA010000")

_HASH_CHUNK = 1 << 20
_META_FLUSH_BYTES = 8 * 1024 * 1024
_META_FLUSH_SECONDS = 2.0
_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)")


class SizeMismatchError(DownloadError):
    """The stored object size does not match the catalog's ``Size`` field."""


class Md5MismatchError(DownloadError):
    """The downloaded bytes do not match the CDN's ``x-goog-hash`` MD5."""


class TransformError(DownloadError):
    """A downloaded object could not be decoded into its final form."""


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Outcome of one asset download."""

    status: str  # "downloaded" or "skipped"
    size: int  # transformed output size
    raw_size: int  # bytes received from the CDN
    verified: bool
    raw_md5: str | None = None
    sha256: str | None = None
    etag: str | None = None
    resumed: bool = False


class _Writer(Protocol):
    def write(self, data: bytes) -> int: ...


class _HashingWriter:
    """Write-through wrapper that hashes exactly the bytes written."""

    def __init__(self, handle: BinaryIO, hasher: Any) -> None:
        """Wrap ``handle`` so every written byte also feeds ``hasher``."""
        self._handle = handle
        self._hasher = hasher

    def write(self, data: bytes) -> int:
        """Hash ``data`` and forward it to the underlying handle."""
        self._hasher.update(data)
        return self._handle.write(data)


class _Lz4StreamDecoder:
    """Incremental LZ4 frame decoder that tolerates concatenated frames."""

    __slots__ = ("_completed_frames", "_decoder", "_last_frame_complete")

    def __init__(self) -> None:
        """Start a fresh decoder with no completed frames."""
        self._decoder = lz4.frame.LZ4FrameDecompressor()
        self._completed_frames = 0
        self._last_frame_complete = False

    def feed(self, data: bytes) -> bytes:
        """Decode ``data`` and return whatever plaintext is available."""
        output = bytearray()
        while data:
            output += self._decoder.decompress(data)
            if self._decoder.eof:
                self._completed_frames += 1
                self._last_frame_complete = True
                data = self._decoder.unused_data
                self._decoder = lz4.frame.LZ4FrameDecompressor()
                continue

            self._last_frame_complete = False
            if self._decoder.needs_input:
                break

            # With max_length=-1 this should not normally happen, but drain
            # buffered output defensively instead of spinning forever.
            output += self._decoder.decompress(b"")
            if self._decoder.eof:
                self._completed_frames += 1
                self._last_frame_complete = True
                data = self._decoder.unused_data
                self._decoder = lz4.frame.LZ4FrameDecompressor()
            else:
                break
        return bytes(output)

    def finish(self) -> None:
        """Raise if the stream ended mid-frame."""
        if self._completed_frames == 0 or not self._last_frame_complete:
            raise ValueError("truncated LZ4 frame")


def _raw_encrypted_key(seed: int, label_crc: int, size: int) -> tuple[bytes, bytes]:
    material = (
        seed.to_bytes(8, "big") + label_crc.to_bytes(8, "big") + size.to_bytes(8, "big")
    )
    digest = hashlib.sha1(material).digest()
    return digest[:16], digest[4:20]


def _copy_chunks(
    chunks: Iterable[bytes],
    output: _Writer,
    *,
    stop_event: threading.Event | None = None,
) -> int:
    total = 0
    for chunk in chunks:
        if stop_event is not None and stop_event.is_set():
            raise DownloadCancelled("cancelled while writing asset")
        output.write(chunk)
        total += len(chunk)
    return total


def _iter_file(
    path: Path,
    stop_event: threading.Event | None = None,
) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            if stop_event is not None and stop_event.is_set():
                raise DownloadCancelled(f"cancelled while reading {path}")
            yield chunk


def _file_md5(path: Path) -> bytes:
    hasher = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            hasher.update(chunk)
    return hasher.digest()


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            hasher.update(chunk)
    return hasher.hexdigest()


def _try_file_sha256(path: Path) -> str | None:
    try:
        return _file_sha256(path)
    except OSError as exc:
        LOGGER.warning("cannot hash %s: %s", path, exc)
        return None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _parse_content_range(value: str | None) -> tuple[int, int, int | None] | None:
    if not value:
        return None
    match = _CONTENT_RANGE_RE.fullmatch(value.strip())
    if match is None:
        return None
    start = int(match.group(1))
    end = int(match.group(2))
    total = None if match.group(3) == "*" else int(match.group(3))
    return start, end, total


def _header_values(headers: httpx.Headers, name: str) -> list[str]:
    return headers.get_list(name)


def _parse_goog_hash(headers: httpx.Headers) -> tuple[str | None, str | None]:
    """Return ``(md5_base64, crc32c_base64)`` from ``x-goog-hash`` headers."""
    md5_value: str | None = None
    crc32c_value: str | None = None
    for header in _header_values(headers, "x-goog-hash"):
        for part in header.split(","):
            key, _, value = part.strip().partition("=")
            if key.lower() == "md5":
                md5_value = value
            elif key.lower() == "crc32c":
                crc32c_value = value
    return md5_value, crc32c_value


def _etag_md5_hex(etag: str | None) -> str | None:
    if not etag:
        return None
    value = etag.strip()
    value = value.removeprefix("W/")
    value = value.strip('"')
    if re.fullmatch(r"[0-9a-fA-F]{32}", value):
        return value.lower()
    return None


class AssetDownloader:
    """Download and transform one catalog asset at a time."""

    def __init__(
        self,
        client: HttpClient,
        *,
        force: bool = False,
        verify: bool = False,
        deep: bool = False,
        resume: bool = True,
        previous_entries: dict[str, dict[str, Any]] | None = None,
        previous_sizes: dict[str, int] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Configure a downloader bound to ``client``.

        Args:
            client: HTTP client used for streaming downloads.
            force: Re-download even when the local file looks valid.
            verify: Re-download when the transformed size is not derivable.
            deep: Re-hash existing files to confirm content identity.
            resume: Resume interrupted ``.part.raw`` transfers.
            previous_entries: Prior sync-state entries keyed by relative path.
            previous_sizes: Legacy size-only mapping; lacks content identity.
            stop_event: Cooperative cancellation signal.
        """
        self.client = client
        self.force = force
        self.verify = verify
        self.deep = deep
        self.resume = resume
        self.stop_event = stop_event
        if previous_entries is not None:
            self.previous_entries = previous_entries
        else:
            # Compatibility for callers that only have sizes.  Size-only
            # entries have no content identity, so existing files are
            # re-downloaded instead of being trusted.
            self.previous_entries = {
                path: {"size": size} for path, size in (previous_sizes or {}).items()
            }

    @staticmethod
    def _expected_transformed_size(entry: CatalogEntry) -> int | None:
        """Return the expected output size when it is derivable.

        The catalog ``Size`` is the **stored object size** (the bytes on the
        CDN), not the decompressed size.  It therefore matches the transformed
        file only for plain resources and, after removing the 4-byte prefix,
        for padded bundles.  For LZ4/encrypted resources the plaintext size is
        only known after decompression.
        """
        if entry.resource_type in (ResourceType.BundlePlain, ResourceType.RawPlain):
            return entry.size
        if entry.resource_type is ResourceType.BundlePaddedStart:
            return max(0, entry.size - 4)
        return None

    def _existing_is_valid(
        self, path: Path, asset: CatalogAsset
    ) -> tuple[bool, str | None]:
        """Return ``(valid, sha256)`` for an existing final file."""
        try:
            stat = path.stat()
        except OSError:
            return False, None
        if not path.is_file():
            return False, None

        recorded = self.previous_entries.get(asset.relative_path)
        if recorded is not None:
            status = recorded.get("status")
            if status in ("failed", "cancelled"):
                return False, None
            recorded_name = recorded.get("realName")
            if not isinstance(recorded_name, str) or recorded_name != asset.real_name:
                # Either the state predates content-identity tracking, or the
                # catalog entry changed.  Re-download so the local file is
                # known to match the current catalog object.
                return False, None
            recorded_size = recorded.get("size")
            if not isinstance(recorded_size, int) or stat.st_size != recorded_size:
                return False, None

            recorded_sha256 = recorded.get("sha256")
            if self.deep:
                if isinstance(recorded_sha256, str):
                    actual = _try_file_sha256(path)
                    if actual is None or actual != recorded_sha256:
                        LOGGER.warning("content hash mismatch, re-downloading %s", path)
                        return False, None
                    return True, actual
                # Legacy entry: establish a baseline instead of re-downloading
                # the whole library just to add hashes.
                actual = _try_file_sha256(path)
                if actual is None:
                    return False, None
                return True, actual
            return True, recorded_sha256 if isinstance(recorded_sha256, str) else None

        expected = self._expected_transformed_size(asset.entry)
        if expected is not None:
            if stat.st_size != expected:
                return False, None
            if self.deep:
                actual = _try_file_sha256(path)
                if actual is None:
                    return False, None
                return True, actual
            return True, None

        # LZ4/encrypted output size is not derivable from the catalog.  With
        # --verify, re-download so the stored size can be checked; otherwise a
        # non-empty file is the best available heuristic.
        if self.verify:
            return False, None
        if stat.st_size <= 0:
            return False, None
        if self.deep:
            actual = _try_file_sha256(path)
            if actual is None:
                return False, None
            return True, actual
        return True, None

    def classify_existing(self, asset: CatalogAsset) -> str:
        """Return ``added``, ``changed`` or ``unchanged`` without writing."""
        output = Path(asset.output_path)
        if not output.exists():
            return "added"
        if self.force:
            return "changed"
        valid, _sha256 = self._existing_is_valid(output, asset)
        return "unchanged" if valid else "changed"

    @staticmethod
    def _read_meta(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _unlink_quietly(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            LOGGER.warning("cannot remove %s: %s", path, exc)

    @classmethod
    def _discard_parts(cls, *paths: Path) -> None:
        for path in paths:
            cls._unlink_quietly(path)

    def _prepare_resume(
        self,
        raw_part: Path,
        meta_part: Path,
        asset: CatalogAsset,
    ) -> int:
        if not self.resume:
            return 0
        if not raw_part.is_file() or not meta_part.is_file():
            return 0
        meta = self._read_meta(meta_part)
        if meta is None:
            self._discard_parts(raw_part, meta_part)
            return 0
        if meta.get("realName") != asset.real_name:
            self._discard_parts(raw_part, meta_part)
            return 0
        raw_size = meta.get("rawSize")
        downloaded = meta.get("downloaded")
        if not isinstance(raw_size, int) or raw_size != asset.entry.size:
            self._discard_parts(raw_part, meta_part)
            return 0
        if (
            not isinstance(downloaded, int)
            or downloaded < 0
            or downloaded > asset.entry.size
        ):
            self._discard_parts(raw_part, meta_part)
            return 0
        try:
            actual = raw_part.stat().st_size
        except OSError:
            self._discard_parts(raw_part, meta_part)
            return 0
        # The metadata write and the data write are not atomic together.
        # Trust the bytes that actually made it to disk.
        downloaded = min(actual, asset.entry.size)
        if downloaded == 0:
            return 0
        return downloaded

    def _download_raw(
        self,
        asset: CatalogAsset,
        raw_part: Path,
        meta_part: Path,
        offset: int,
    ) -> tuple[int, str | None, str | None, bool]:
        entry = asset.entry
        meta = self._read_meta(meta_part) or {}
        stored_etag = _optional_str(meta.get("etag"))
        stored_md5 = _optional_str(meta.get("rawMd5"))

        if offset >= entry.size:
            raw_size = raw_part.stat().st_size
            if raw_size != entry.size:
                raise SizeMismatchError(
                    f"{asset.output_path}: expected {entry.size} stored bytes, "
                    f"got {raw_size}"
                )
            raw_md5 = self._verify_raw_md5(raw_part, stored_md5, stored_etag)
            return raw_size, raw_md5, stored_etag, True

        url = build_asset_url(asset.real_name, self.client.base_url)
        headers: dict[str, str] | None = None
        if offset > 0:
            LOGGER.debug("resuming %s at %d/%d bytes", url, offset, entry.size)
            headers = {"Range": f"bytes={offset}-"}

        md5_b64: str | None = None
        etag: str | None = None
        with self.client.stream_response(url, headers=headers) as (response, chunks):
            status = response.status_code
            etag = response.headers.get("etag")
            if status == 206:
                content_range = _parse_content_range(
                    response.headers.get("content-range")
                )
                if content_range is None:
                    raise RangeNotSatisfiableError(
                        f"206 response without a valid Content-Range for {url}"
                    )
                start, end, total = content_range
                if start != offset:
                    raise RangeNotSatisfiableError(
                        f"server resumed at {start}, expected {offset} for {url}"
                    )
                # A range must be non-empty and may not claim bytes beyond the
                # object; either shape means the response cannot be trusted.
                if end < start:
                    raise RangeNotSatisfiableError(
                        f"server sent an empty range {start}-{end} for {url}"
                    )
                if end >= entry.size:
                    raise RangeNotSatisfiableError(
                        f"{url}: range ends at {end}, past the {entry.size}-byte object"
                    )
                expected_body = end - start + 1
                if total is not None and total != entry.size:
                    raise SizeMismatchError(
                        f"{url}: server reports {total} stored bytes, "
                        f"catalog says {entry.size}"
                    )
                mode = "ab" if offset > 0 else "wb"
            elif status == 200:
                if offset > 0:
                    LOGGER.debug("server ignored Range for %s; restarting", url)
                    offset = 0
                mode = "wb"
            else:  # pragma: no cover - _raise_for_status handles other statuses
                raise DownloadError(f"unexpected HTTP {status} for {url}")

            resumed = offset > 0
            if offset > 0 and stored_etag and etag and etag != stored_etag:
                raise RangeNotSatisfiableError(
                    f"object changed on the server for {url}: {stored_etag} -> {etag}"
                )

            stored_length = response.headers.get("x-goog-stored-content-length")
            if stored_length is not None:
                try:
                    stored_size = int(stored_length)
                except ValueError:
                    stored_size = None
                if stored_size is not None and stored_size != entry.size:
                    raise SizeMismatchError(
                        f"{url}: server reports {stored_size} stored bytes, "
                        f"catalog says {entry.size}"
                    )

            md5_b64, _crc32c = _parse_goog_hash(response.headers)
            meta = {
                "realName": asset.real_name,
                "rawSize": entry.size,
                "downloaded": offset,
                "etag": etag,
                "lastModified": response.headers.get("last-modified"),
                "rawMd5": md5_b64 or stored_md5,
            }
            write_json_atomic(meta_part, meta)

            downloaded = offset
            last_flush = time.monotonic()
            last_flush_bytes = downloaded
            with raw_part.open(mode) as output:
                for chunk in chunks:
                    if self.stop_event is not None and self.stop_event.is_set():
                        raise DownloadCancelled(f"cancelled while downloading {url}")
                    output.write(chunk)
                    downloaded += len(chunk)
                    now = time.monotonic()
                    if (
                        downloaded - last_flush_bytes >= _META_FLUSH_BYTES
                        or now - last_flush >= _META_FLUSH_SECONDS
                    ):
                        output.flush()
                        meta["downloaded"] = downloaded
                        write_json_atomic(meta_part, meta)
                        last_flush = now
                        last_flush_bytes = downloaded
                output.flush()
            meta["downloaded"] = downloaded
            write_json_atomic(meta_part, meta)

            if status == 206 and downloaded - offset != expected_body:
                raise SizeMismatchError(
                    f"{url}: Content-Range promised {expected_body} bytes, "
                    f"received {downloaded - offset}"
                )

        raw_size = raw_part.stat().st_size
        if raw_size != entry.size:
            raise SizeMismatchError(
                f"{url}: expected {entry.size} stored bytes, got {raw_size}"
            )
        raw_md5 = self._verify_raw_md5(
            raw_part,
            md5_b64 or stored_md5,
            etag or stored_etag,
        )
        return raw_size, raw_md5, etag or stored_etag, resumed

    def _verify_raw_md5(
        self,
        path: Path,
        expected_b64: str | None,
        etag: str | None,
    ) -> str:
        digest = _file_md5(path)
        actual_hex = digest.hex()
        if expected_b64:
            try:
                expected = base64.b64decode(expected_b64, validate=True)
            except Exception as exc:  # binascii.Error is not stable
                raise Md5MismatchError(
                    f"{path}: invalid x-goog-hash md5 value {expected_b64!r}: {exc}"
                ) from exc
            if digest != expected:
                raise Md5MismatchError(
                    f"{path}: MD5 mismatch (expected {expected_b64}, got "
                    f"{base64.b64encode(digest).decode('ascii')})"
                )
        else:
            expected_hex = _etag_md5_hex(etag)
            if expected_hex is not None and actual_hex != expected_hex:
                raise Md5MismatchError(
                    f"{path}: MD5 mismatch (expected {expected_hex}, got {actual_hex})"
                )
        return actual_hex

    @staticmethod
    def _check_padded_prefix(path: Path) -> None:
        with path.open("rb") as handle:
            prefix = handle.read(len(PADDED_BUNDLE_PREFIX))
        if prefix != PADDED_BUNDLE_PREFIX:
            raise TransformError(
                f"{path}: unexpected padded bundle prefix {prefix.hex()!r}; "
                f"expected {PADDED_BUNDLE_PREFIX.hex()!r}"
            )

    def _transform(self, asset: CatalogAsset, raw_part: Path, tmp_part: Path) -> str:
        entry = asset.entry
        resource_type = entry.resource_type
        if resource_type is None:
            raise UnsupportedResourceType(entry.resource_type_value)

        try:
            if resource_type in (ResourceType.BundlePlain, ResourceType.RawPlain):
                # The stored object is already the final file; rename instead
                # of copying it, then hash it for future --deep runs.
                raw_part.replace(tmp_part)
                return _file_sha256(tmp_part)

            hasher = hashlib.sha256()
            with tmp_part.open("wb") as handle:
                writer = _HashingWriter(handle, hasher)
                if resource_type is ResourceType.BundlePaddedStart:
                    self._check_padded_prefix(raw_part)
                    with raw_part.open("rb") as source:
                        source.seek(len(PADDED_BUNDLE_PREFIX))
                        while chunk := source.read(_HASH_CHUNK):
                            if self.stop_event is not None and self.stop_event.is_set():
                                raise DownloadCancelled(
                                    f"cancelled while transforming {asset.output_path}"
                                )
                            writer.write(chunk)
                else:
                    self._write_transformed(
                        entry,
                        _iter_file(raw_part, self.stop_event),
                        writer,
                        stop_event=self.stop_event,
                    )
                handle.flush()
            return hasher.hexdigest()
        except DownloadCancelled:
            self._unlink_quietly(tmp_part)
            raise
        except OSError:
            self._unlink_quietly(tmp_part)
            raise
        except Exception as exc:
            self._unlink_quietly(tmp_part)
            if isinstance(exc, (UnsupportedResourceType, DownloadError)):
                raise
            raise TransformError(
                f"failed to transform {asset.output_path}: {exc}"
            ) from exc

    def download(self, asset: CatalogAsset) -> DownloadResult:
        """Download one asset, retrying transient failures.

        Raises:
            NotFoundError: the object is absent from the CDN.
            DownloadError: a non-retryable HTTP, size, MD5 or transform failure.
            DownloadCancelled: the caller set ``stop_event``; partial files are
                kept so the next run can resume.
            UnsupportedResourceType: the catalog uses an unknown type.
        """
        output = Path(asset.output_path)
        if not self.force and output.exists():
            valid, sha256 = self._existing_is_valid(output, asset)
            if valid:
                recorded = self.previous_entries.get(asset.relative_path, {})
                return DownloadResult(
                    status="skipped",
                    size=output.stat().st_size,
                    raw_size=asset.entry.size,
                    verified=False,
                    raw_md5=_optional_str(recorded.get("rawMd5")),
                    sha256=sha256 or _optional_str(recorded.get("sha256")),
                    etag=_optional_str(recorded.get("etag")),
                )

        output.parent.mkdir(parents=True, exist_ok=True)
        raw_part = output.with_name(output.name + PART_RAW_SUFFIX)
        meta_part = output.with_name(output.name + PART_META_SUFFIX)
        tmp_part = output.with_name(output.name + PART_TMP_SUFFIX)

        if self.force or not self.resume:
            self._discard_parts(raw_part, meta_part, tmp_part)

        attempts = self.client.retries
        last_error: Exception | None = None

        for attempt in range(attempts + 1):
            try:
                offset = self._prepare_resume(raw_part, meta_part, asset)
                raw_size, raw_md5, etag, resumed = self._download_raw(
                    asset, raw_part, meta_part, offset
                )
                sha256 = self._transform(asset, raw_part, tmp_part)
                size = tmp_part.stat().st_size
                tmp_part.replace(output)
                self._discard_parts(raw_part, meta_part)
                return DownloadResult(
                    status="downloaded",
                    size=size,
                    raw_size=raw_size,
                    verified=True,
                    raw_md5=raw_md5,
                    sha256=sha256,
                    etag=etag,
                    resumed=resumed,
                )
            except DownloadCancelled:
                raise
            except NotFoundError:
                self._discard_parts(raw_part, meta_part, tmp_part)
                raise
            except (RetryableHTTPError, httpx.TransportError) as exc:
                # Keep the partial raw file: the next attempt resumes it.
                last_error = exc
                if attempt >= attempts:
                    break
                retry_after = (
                    exc.retry_after if isinstance(exc, RetryableHTTPError) else None
                )
                self.client._sleep_before_retry(attempt, retry_after)
            except (
                SizeMismatchError,
                Md5MismatchError,
                TransformError,
                RangeNotSatisfiableError,
            ) as exc:
                # The partial data cannot be trusted; start this object over.
                last_error = exc
                self._discard_parts(raw_part, meta_part, tmp_part)
                if attempt >= attempts:
                    break
                self.client._sleep_before_retry(attempt, None)
            except DownloadError:
                self._discard_parts(raw_part, meta_part, tmp_part)
                raise
            except Exception:
                self._discard_parts(raw_part, meta_part, tmp_part)
                raise

        assert last_error is not None
        raise DownloadError(
            f"failed to download {asset.output_path} after {attempts + 1} attempts: "
            f"{last_error}"
        ) from last_error

    def _write_transformed(
        self,
        entry: CatalogEntry,
        chunks: Iterable[bytes],
        output: _Writer,
        *,
        stop_event: threading.Event | None = None,
    ) -> int:
        resource_type = entry.resource_type
        if resource_type is None:
            raise UnsupportedResourceType(entry.resource_type_value)

        if resource_type in (ResourceType.BundlePlain, ResourceType.RawPlain):
            return _copy_chunks(chunks, output, stop_event=stop_event)

        if resource_type is ResourceType.BundlePaddedStart:
            skip = 4
            raw_size = 0
            for chunk in chunks:
                if stop_event is not None and stop_event.is_set():
                    raise DownloadCancelled("cancelled while writing padded bundle")
                raw_size += len(chunk)
                if skip:
                    if len(chunk) <= skip:
                        skip -= len(chunk)
                        continue
                    chunk = chunk[skip:]
                    skip = 0
                output.write(chunk)
            if skip:
                raise ValueError("padded bundle is shorter than 4 bytes")
            return raw_size

        if resource_type is ResourceType.RawLz4Compressed:
            decoder = _Lz4StreamDecoder()
            raw_size = 0
            for chunk in chunks:
                if stop_event is not None and stop_event.is_set():
                    raise DownloadCancelled("cancelled while decompressing LZ4 data")
                raw_size += len(chunk)
                output.write(decoder.feed(chunk))
            decoder.finish()
            return raw_size

        if resource_type is ResourceType.RawEncrypted:
            key, iv = _raw_encrypted_key(entry.seed, entry.label_crc, entry.size)
            decryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
            decoder = _Lz4StreamDecoder()
            raw_size = 0
            for chunk in chunks:
                if stop_event is not None and stop_event.is_set():
                    raise DownloadCancelled("cancelled while decrypting asset")
                raw_size += len(chunk)
                output.write(decoder.feed(decryptor.update(chunk)))
            tail = decryptor.finalize()
            if tail:
                output.write(decoder.feed(tail))
            decoder.finish()
            return raw_size

        raise UnsupportedResourceType(int(resource_type))


def download_assets(
    downloader: AssetDownloader,
    assets: Sequence[CatalogAsset],
    *,
    jobs: int = 8,
    on_progress: Callable[[CatalogAsset, int, int], None] | None = None,
) -> list[DownloadResult | Exception | None]:
    """Download ``assets`` concurrently through ``downloader``.

    Every asset gets exactly one task.  Results follow input order, so
    ``results[i]`` always describes ``assets[i]`` regardless of completion
    order.

    The batch shares ``downloader.stop_event`` as its single cancellation
    signal: the scheduler checks it before starting each task and the running
    downloads poll it through the same attribute.  When the downloader has no
    event, a fresh one is installed on the downloader for the call.

    Args:
        downloader: Downloader used for every asset.
        assets: Assets to download, in the order results should follow.
        jobs: Maximum number of concurrent downloads.  Must satisfy
            ``1 <= jobs <= MAX_JOBS``.
        on_progress: Optional callback invoked from the scheduling thread after
            each asset finishes.  It receives the finished asset, the number of
            processed assets so far and the batch size.  Exceptions raised by
            the callback cancel the batch and propagate unchanged.

    Returns:
        A list aligned with ``assets``:

        * ``DownloadResult`` for a downloaded or skipped asset;
        * the original ``Exception`` instance for a failed asset;
        * ``None`` for an asset that never started because the batch was
          cancelled.

    Raises:
        ValueError: If ``jobs`` is outside the allowed range.
        BaseException: ``KeyboardInterrupt`` or an ``on_progress`` failure.  In
            both cases the shared stop event is set, pending tasks are
            cancelled, running tasks are awaited and the original exception is
            re-raised unchanged.
    """
    if not 1 <= jobs <= MAX_JOBS:
        raise ValueError(f"jobs must be between 1 and {MAX_JOBS}")

    total = len(assets)
    results: list[DownloadResult | Exception | None] = [None] * total
    if total == 0:
        return results

    stop_event = downloader.stop_event
    if stop_event is None:
        stop_event = threading.Event()
        downloader.stop_event = stop_event

    executor = ThreadPoolExecutor(max_workers=jobs)
    futures: dict[Future[DownloadResult], int] = {}
    processed = 0
    try:
        for index, asset in enumerate(assets):
            if stop_event.is_set():
                break
            futures[executor.submit(downloader.download, asset)] = index

        for future in as_completed(list(futures)):
            index = futures[future]
            try:
                outcome: DownloadResult | Exception = future.result()
            except Exception as exc:
                outcome = exc

            if isinstance(outcome, DownloadCancelled):
                # A cancellation raised without the shared event set is a real
                # error of the asset, not a batch-level stop.
                results[index] = outcome
                if not stop_event.is_set():
                    processed += 1
                    if on_progress is not None:
                        on_progress(assets[index], processed, total)
                continue

            results[index] = outcome
            processed += 1
            if on_progress is not None:
                on_progress(assets[index], processed, total)
    except BaseException:
        stop_event.set()
        for pending in futures:
            pending.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)

    return results
