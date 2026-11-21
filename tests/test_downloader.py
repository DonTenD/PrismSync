"""Tests for the single-asset downloader."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import lz4.frame
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from prism_sync._fsutil import write_json_atomic
from prism_sync.downloader import (
    PADDED_BUNDLE_PREFIX,
    PART_META_SUFFIX,
    PART_RAW_SUFFIX,
    PART_TMP_SUFFIX,
    AssetDownloader,
    DownloadResult,
    Md5MismatchError,
    SizeMismatchError,
    TransformError,
    _etag_md5_hex,
    _Lz4StreamDecoder,
    _parse_content_range,
    _parse_goog_hash,
)
from prism_sync.errors import DownloadCancelled
from prism_sync.http_client import (
    DownloadError,
    NotFoundError,
    RangeNotSatisfiableError,
    RetryableHTTPError,
)
from prism_sync.models import CatalogAsset, CatalogEntry, ResourceType

# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


class FakeResponse:
    """Minimal stand-in for ``httpx.Response`` used by the fake client."""

    def __init__(
        self,
        status_code: int,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = httpx.Headers(headers or {})

    def iter_bytes(self, chunk_size: int = 1 << 20) -> Any:
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start : start + chunk_size]


class FakeHttpClient:
    """Scripted ``HttpClient`` that records every request."""

    def __init__(
        self,
        responses: list[Any],
        *,
        retries: int = 3,
        base_url: str = "https://cdn.example",
        chunk_size: int = 1 << 20,
    ) -> None:
        self.responses = list(responses)
        self.retries = retries
        self.base_url = base_url
        self.chunk_size = chunk_size
        self.requests: list[dict[str, Any]] = []
        self.sleeps: list[tuple[int, float | None]] = []

    def stream_response(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        chunk_size: int = 1 << 20,
    ) -> Any:
        self.requests.append({"url": url, "headers": dict(headers or {})})
        chunk_size = min(chunk_size, self.chunk_size)
        if not self.responses:
            raise AssertionError("unexpected extra HTTP request")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        response: FakeResponse = item
        return self._context(response, chunk_size)

    @contextmanager
    def _context(self, response: FakeResponse, chunk_size: int) -> Any:
        yield response, response.iter_bytes(chunk_size)

    def _sleep_before_retry(self, attempt: int, retry_after: float | None) -> None:
        self.sleeps.append((attempt, retry_after))


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def make_entry(
    size: int,
    *,
    resource_type: ResourceType = ResourceType.RawPlain,
    label: str = "file",
    seed: int = 0,
    label_crc: int = 0,
) -> CatalogEntry:
    return CatalogEntry(
        label=label,
        type_name="asset",
        size=size,
        checksum=0,
        seed=seed,
        label_crc=label_crc,
        priority=0,
        resource_type=resource_type,
        resource_type_value=int(resource_type),
    )


def make_asset(
    tmp_path: Path, entry: CatalogEntry, name: str = "file.bin"
) -> CatalogAsset:
    output = tmp_path / "out" / name
    return CatalogAsset(
        entry=entry,
        catalog_label="root",
        output_path=str(output),
        relative_path=f"out/{name}",
        real_name="abcdef0123456789",
    )


def part_paths(asset: CatalogAsset) -> tuple[Path, Path, Path]:
    output = Path(asset.output_path)
    return (
        output.with_name(output.name + PART_RAW_SUFFIX),
        output.with_name(output.name + PART_META_SUFFIX),
        output.with_name(output.name + PART_TMP_SUFFIX),
    )


def ok_response(body: bytes, **headers: str) -> FakeResponse:
    merged = {"etag": hashlib.md5(body, usedforsecurity=False).hexdigest()}
    merged.update(headers)
    return FakeResponse(200, body, merged)


def partial_response(
    body: bytes,
    *,
    start: int,
    total: int,
    full_body: bytes | None = None,
    **headers: str,
) -> FakeResponse:
    """Build a 206 response whose ETag describes the whole object.

    ``body`` is the ranged slice; ``full_body`` (when given) is used to derive
    the object-level ETag, matching how GCS reports a whole-object MD5.
    """
    end = start + len(body) - 1
    etag_source = body if full_body is None else full_body
    merged = {
        "content-range": f"bytes {start}-{end}/{total}",
        "etag": hashlib.md5(etag_source, usedforsecurity=False).hexdigest(),
    }
    merged.update(headers)
    return FakeResponse(206, body, merged)


# --------------------------------------------------------------------------
# Helper function tests
# --------------------------------------------------------------------------


class TestParseContentRange:
    """Tests for ``_parse_content_range``."""

    def test_valid_with_total(self) -> None:
        assert _parse_content_range("bytes 0-99/100") == (0, 99, 100)

    def test_valid_with_star_total(self) -> None:
        assert _parse_content_range("bytes 0-99/*") == (0, 99, None)

    def test_extra_whitespace(self) -> None:
        assert _parse_content_range("  bytes   0-99/100  ") == (0, 99, 100)

    @pytest.mark.parametrize(
        "value",
        [None, "", "bytes 0-99", "bytes=0-99/100", "bytes 0-99/100, 200-299/300"],
    )
    def test_invalid(self, value: str | None) -> None:
        assert _parse_content_range(value) is None


class TestParseGoogHash:
    """Tests for ``_parse_goog_hash``."""

    def test_md5_and_crc32c(self) -> None:
        headers = httpx.Headers({"x-goog-hash": "crc32c=AAAAAA==,md5=BBBBBB=="})
        assert _parse_goog_hash(headers) == ("BBBBBB==", "AAAAAA==")

    def test_missing(self) -> None:
        assert _parse_goog_hash(httpx.Headers({})) == (None, None)


class TestEtagMd5Hex:
    """Tests for ``_etag_md5_hex``."""

    def test_plain_hex(self) -> None:
        assert _etag_md5_hex("a" * 32) == "a" * 32

    def test_quoted_and_weak(self) -> None:
        assert _etag_md5_hex('W/"' + "b" * 32 + '"') == "b" * 32

    def test_non_md5(self) -> None:
        assert _etag_md5_hex("not-a-md5") is None
        assert _etag_md5_hex(None) is None


# --------------------------------------------------------------------------
# Basic download behaviour
# --------------------------------------------------------------------------


class TestBasicDownload:
    """Tests for the happy path."""

    def test_download_raw_plain(self, tmp_path: Path) -> None:
        body = b"hello world"
        asset = make_asset(tmp_path, make_entry(len(body)))
        client = FakeHttpClient([ok_response(body)])

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert result.status == "downloaded"
        assert result.size == len(body)
        assert result.raw_size == len(body)
        assert result.verified is True
        assert result.resumed is False
        assert Path(asset.output_path).read_bytes() == body
        assert len(client.requests) == 1
        assert "Range" not in client.requests[0]["headers"]
        assert "If-Range" not in client.requests[0]["headers"]

        raw, meta, tmp = part_paths(asset)
        assert not raw.exists()
        assert not meta.exists()
        assert not tmp.exists()

    def test_skip_when_existing_file_is_valid(self, tmp_path: Path) -> None:
        body = b"cached"
        asset = make_asset(tmp_path, make_entry(len(body)))
        Path(asset.output_path).parent.mkdir(parents=True)
        Path(asset.output_path).write_bytes(body)
        client = FakeHttpClient([])

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert result.status == "skipped"
        assert result.size == len(body)
        assert result.verified is False
        assert client.requests == []

    def test_force_ignores_existing_file(self, tmp_path: Path) -> None:
        body = b"fresh"
        asset = make_asset(tmp_path, make_entry(len(body)))
        Path(asset.output_path).parent.mkdir(parents=True)
        Path(asset.output_path).write_bytes(b"stale")
        client = FakeHttpClient([ok_response(body)])

        result = AssetDownloader(client, force=True).download(asset)  # type: ignore[arg-type]

        assert result.status == "downloaded"
        assert Path(asset.output_path).read_bytes() == body
        assert len(client.requests) == 1

    def test_classify_existing(self, tmp_path: Path) -> None:
        asset = make_asset(tmp_path, make_entry(3))
        downloader = AssetDownloader(FakeHttpClient([]))  # type: ignore[arg-type]
        assert downloader.classify_existing(asset) == "added"

        Path(asset.output_path).parent.mkdir(parents=True)
        Path(asset.output_path).write_bytes(b"abc")
        assert downloader.classify_existing(asset) == "unchanged"

        force = AssetDownloader(FakeHttpClient([]), force=True)  # type: ignore[arg-type]
        assert force.classify_existing(asset) == "changed"


# --------------------------------------------------------------------------
# Resume behaviour
# --------------------------------------------------------------------------


class TestResume:
    """Tests for ``.part.raw`` / ``.part.json`` resume handling."""

    def _seed_parts(
        self,
        asset: CatalogAsset,
        raw: bytes,
        *,
        downloaded: int | None = None,
        real_name: str | None = None,
        raw_size: int | None = None,
        etag: str | None = None,
    ) -> None:
        raw_part, meta_part, _ = part_paths(asset)
        raw_part.parent.mkdir(parents=True, exist_ok=True)
        raw_part.write_bytes(raw)
        meta = {
            "realName": real_name if real_name is not None else asset.real_name,
            "rawSize": raw_size if raw_size is not None else asset.entry.size,
            "downloaded": downloaded if downloaded is not None else len(raw),
            "etag": etag,
            "lastModified": None,
            "rawMd5": None,
        }
        meta_part.write_text(json.dumps(meta), encoding="utf-8")

    def test_resume_sends_range_and_appends(self, tmp_path: Path) -> None:
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed_parts(asset, body[:4], etag="etag-1")
        client = FakeHttpClient(
            [
                partial_response(
                    body[4:], start=4, total=len(body), full_body=body, etag="etag-1"
                )
            ]
        )

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert result.status == "downloaded"
        assert result.resumed is True
        assert client.requests[0]["headers"]["Range"] == "bytes=4-"
        assert "If-Range" not in client.requests[0]["headers"]
        assert Path(asset.output_path).read_bytes() == body

    def test_actual_size_overrides_metadata_downloaded(self, tmp_path: Path) -> None:
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        # Metadata claims 2 bytes but 5 are actually on disk.
        self._seed_parts(asset, body[:5], downloaded=2)
        client = FakeHttpClient(
            [partial_response(body[5:], start=5, total=len(body), full_body=body)]
        )

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert result.resumed is True
        assert client.requests[0]["headers"]["Range"] == "bytes=5-"
        assert Path(asset.output_path).read_bytes() == body

    def test_corrupt_metadata_discards_parts(self, tmp_path: Path) -> None:
        body = b"abcdef"
        asset = make_asset(tmp_path, make_entry(len(body)))
        raw_part, meta_part, _ = part_paths(asset)
        raw_part.parent.mkdir(parents=True)
        raw_part.write_bytes(b"ab")
        meta_part.write_text("not json", encoding="utf-8")
        client = FakeHttpClient([ok_response(body)])

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert result.resumed is False
        assert "Range" not in client.requests[0]["headers"]
        assert Path(asset.output_path).read_bytes() == body

    def test_mismatched_real_name_discards_parts(self, tmp_path: Path) -> None:
        body = b"abcdef"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed_parts(asset, b"ab", real_name="different")
        client = FakeHttpClient([ok_response(body)])

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert result.resumed is False
        assert "Range" not in client.requests[0]["headers"]

    def test_mismatched_raw_size_discards_parts(self, tmp_path: Path) -> None:
        body = b"abcdef"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed_parts(asset, b"ab", raw_size=999)
        client = FakeHttpClient([ok_response(body)])

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert result.resumed is False

    def test_negative_downloaded_discards_parts(self, tmp_path: Path) -> None:
        body = b"abcdef"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed_parts(asset, b"ab", downloaded=-1)
        client = FakeHttpClient([ok_response(body)])

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert result.resumed is False

    def test_metadata_without_raw_part_does_not_resume(self, tmp_path: Path) -> None:
        body = b"abcdef"
        asset = make_asset(tmp_path, make_entry(len(body)))
        _, meta_part, _ = part_paths(asset)
        meta_part.parent.mkdir(parents=True)
        meta_part.write_text(
            json.dumps(
                {"realName": asset.real_name, "rawSize": len(body), "downloaded": 2}
            ),
            encoding="utf-8",
        )
        client = FakeHttpClient([ok_response(body)])

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert result.resumed is False
        assert "Range" not in client.requests[0]["headers"]

    def test_resume_false_discards_parts_before_download(self, tmp_path: Path) -> None:
        body = b"abcdef"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed_parts(asset, b"ab")
        client = FakeHttpClient([ok_response(body)])

        result = AssetDownloader(client, resume=False).download(asset)  # type: ignore[arg-type]

        assert result.resumed is False
        assert "Range" not in client.requests[0]["headers"]
        assert Path(asset.output_path).read_bytes() == body


# --------------------------------------------------------------------------
# HTTP status handling
# --------------------------------------------------------------------------


class TestHttpStatusHandling:
    """Tests for 200 / 206 / 416 handling."""

    def test_http_200_after_range_restarts(self, tmp_path: Path) -> None:
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        raw_part, meta_part, _ = part_paths(asset)
        raw_part.parent.mkdir(parents=True)
        raw_part.write_bytes(body[:4])
        meta_part.write_text(
            json.dumps(
                {"realName": asset.real_name, "rawSize": len(body), "downloaded": 4}
            ),
            encoding="utf-8",
        )
        client = FakeHttpClient([ok_response(body)])

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert client.requests[0]["headers"]["Range"] == "bytes=4-"
        assert result.resumed is False
        assert Path(asset.output_path).read_bytes() == body

    def test_206_invalid_start_retries_then_fails(self, tmp_path: Path) -> None:
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        raw_part, meta_part, _ = part_paths(asset)
        raw_part.parent.mkdir(parents=True)
        raw_part.write_bytes(body[:4])
        meta_part.write_text(
            json.dumps(
                {"realName": asset.real_name, "rawSize": len(body), "downloaded": 4}
            ),
            encoding="utf-8",
        )
        client = FakeHttpClient(
            [partial_response(body[6:], start=6, total=len(body)) for _ in range(4)],
            retries=3,
        )

        with pytest.raises(DownloadError) as excinfo:
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert isinstance(excinfo.value.__cause__, RangeNotSatisfiableError)
        assert len(client.requests) == 4
        assert not raw_part.exists()
        assert not meta_part.exists()

    def test_206_total_mismatch_is_size_error(self, tmp_path: Path) -> None:
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        raw_part, meta_part, _ = part_paths(asset)
        raw_part.parent.mkdir(parents=True)
        raw_part.write_bytes(body[:4])
        meta_part.write_text(
            json.dumps(
                {"realName": asset.real_name, "rawSize": len(body), "downloaded": 4}
            ),
            encoding="utf-8",
        )
        client = FakeHttpClient(
            [partial_response(body[4:], start=4, total=999) for _ in range(4)],
            retries=3,
        )

        with pytest.raises(DownloadError) as excinfo:
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        # The first attempt raises SizeMismatchError, which clears the parts.
        # Later attempts then restart at offset 0 and the server still replies
        # with a 206 starting at 4, so the final wrapped error is the range one.
        assert isinstance(
            excinfo.value.__cause__, (SizeMismatchError, RangeNotSatisfiableError)
        )
        assert "999" in str(excinfo.value) or "expected 0" in str(excinfo.value)

    def test_content_range_end_before_start_is_rejected(self, tmp_path: Path) -> None:
        """An impossible range (``end < start``) is refused, not accepted.

        Stage three tightened this check: previously the ``end`` field was
        ignored, which let a server describe an empty range and still have its
        body appended to the partial file.
        """
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        raw_part, meta_part, _ = part_paths(asset)
        raw_part.parent.mkdir(parents=True)
        raw_part.write_bytes(body[:4])
        meta_part.write_text(
            json.dumps(
                {"realName": asset.real_name, "rawSize": len(body), "downloaded": 4}
            ),
            encoding="utf-8",
        )
        response = FakeResponse(
            206,
            body[4:],
            {
                "content-range": f"bytes 4-3/{len(body)}",
                "etag": hashlib.md5(body, usedforsecurity=False).hexdigest(),
            },
        )
        client = FakeHttpClient([response], retries=0)

        with pytest.raises(DownloadError) as excinfo:
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert isinstance(excinfo.value.__cause__, RangeNotSatisfiableError)
        assert not Path(asset.output_path).exists()

    def test_http_416_retries_then_raises(self, tmp_path: Path) -> None:
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        raw_part, meta_part, _ = part_paths(asset)
        raw_part.parent.mkdir(parents=True)
        raw_part.write_bytes(body[:4])
        meta_part.write_text(
            json.dumps(
                {"realName": asset.real_name, "rawSize": len(body), "downloaded": 4}
            ),
            encoding="utf-8",
        )
        client = FakeHttpClient(
            [RangeNotSatisfiableError("416") for _ in range(4)], retries=3
        )

        with pytest.raises(DownloadError) as excinfo:
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert isinstance(excinfo.value.__cause__, RangeNotSatisfiableError)
        assert len(client.requests) == 4
        assert not raw_part.exists()
        assert not meta_part.exists()


# --------------------------------------------------------------------------
# ETag behaviour
# --------------------------------------------------------------------------


class TestEtagBehaviour:
    """Tests for ETag comparison during resume."""

    def _seed(self, asset: CatalogAsset, raw: bytes, etag: str | None) -> None:
        raw_part, meta_part, _ = part_paths(asset)
        raw_part.parent.mkdir(parents=True)
        raw_part.write_bytes(raw)
        meta_part.write_text(
            json.dumps(
                {
                    "realName": asset.real_name,
                    "rawSize": asset.entry.size,
                    "downloaded": len(raw),
                    "etag": etag,
                }
            ),
            encoding="utf-8",
        )

    def test_matching_etag_allows_resume(self, tmp_path: Path) -> None:
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed(asset, body[:4], "same")
        client = FakeHttpClient(
            [
                partial_response(
                    body[4:], start=4, total=len(body), full_body=body, etag="same"
                )
            ]
        )

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert result.resumed is True

    def test_changed_etag_raises_range_error(self, tmp_path: Path) -> None:
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed(asset, body[:4], "old")
        client = FakeHttpClient(
            [
                partial_response(body[4:], start=4, total=len(body), etag="new")
                for _ in range(4)
            ],
            retries=3,
        )

        with pytest.raises(DownloadError) as excinfo:
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert isinstance(excinfo.value.__cause__, RangeNotSatisfiableError)

    def test_missing_stored_etag_skips_comparison(self, tmp_path: Path) -> None:
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed(asset, body[:4], None)
        client = FakeHttpClient(
            [
                partial_response(
                    body[4:], start=4, total=len(body), full_body=body, etag="server"
                )
            ]
        )

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert result.resumed is True
        assert Path(asset.output_path).read_bytes() == body


# --------------------------------------------------------------------------
# Retry / failure classification
# --------------------------------------------------------------------------


class TestRetryBehaviour:
    """Tests for retry counts and part-file lifecycle."""

    def test_transport_error_keeps_parts_and_retries(self, tmp_path: Path) -> None:
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        raw_part, meta_part, _ = part_paths(asset)
        raw_part.parent.mkdir(parents=True)
        raw_part.write_bytes(body[:4])
        meta_part.write_text(
            json.dumps(
                {"realName": asset.real_name, "rawSize": len(body), "downloaded": 4}
            ),
            encoding="utf-8",
        )
        client = FakeHttpClient(
            [
                httpx.ConnectError("boom"),
                httpx.ReadTimeout("slow"),
                partial_response(body[4:], start=4, total=len(body), full_body=body),
            ],
            retries=3,
        )

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert result.status == "downloaded"
        assert len(client.requests) == 3
        assert len(client.sleeps) == 2
        assert Path(asset.output_path).read_bytes() == body

    def test_retry_exhaustion_wraps_last_error(self, tmp_path: Path) -> None:
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        client = FakeHttpClient(
            [httpx.ConnectError("boom") for _ in range(4)], retries=3
        )

        with pytest.raises(DownloadError) as excinfo:
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert isinstance(excinfo.value.__cause__, httpx.ConnectError)
        assert "after 4 attempts" in str(excinfo.value)
        assert len(client.requests) == 4

    def test_retryable_http_error_uses_retry_after(self, tmp_path: Path) -> None:
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        client = FakeHttpClient(
            [
                RetryableHTTPError(503, retry_after=2.5),
                ok_response(body),
            ],
            retries=3,
        )

        AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert client.sleeps == [(0, 2.5)]

    def test_size_mismatch_clears_parts_and_retries(self, tmp_path: Path) -> None:
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        raw_part, meta_part, _ = part_paths(asset)
        # Server returns fewer bytes than the catalog declares.
        client = FakeHttpClient([ok_response(body[:5]) for _ in range(4)], retries=3)

        with pytest.raises(DownloadError) as excinfo:
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert isinstance(excinfo.value.__cause__, SizeMismatchError)
        assert len(client.requests) == 4
        assert not raw_part.exists()
        assert not meta_part.exists()

    def test_md5_mismatch_is_retried_and_cleans_parts(self, tmp_path: Path) -> None:
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        raw_part, meta_part, _ = part_paths(asset)
        bad = FakeResponse(
            200,
            body,
            {"etag": hashlib.md5(b"other", usedforsecurity=False).hexdigest()},
        )
        client = FakeHttpClient([bad for _ in range(4)], retries=3)

        with pytest.raises(DownloadError) as excinfo:
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert isinstance(excinfo.value.__cause__, Md5MismatchError)
        assert not raw_part.exists()
        assert not meta_part.exists()

    def test_not_found_clears_parts_and_raises_immediately(
        self, tmp_path: Path
    ) -> None:
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        raw_part, meta_part, _ = part_paths(asset)
        raw_part.parent.mkdir(parents=True)
        raw_part.write_bytes(body[:4])
        meta_part.write_text("{}", encoding="utf-8")
        client = FakeHttpClient([NotFoundError("404")], retries=3)

        with pytest.raises(NotFoundError):
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert len(client.requests) == 1
        assert not raw_part.exists()
        assert not meta_part.exists()

    def test_download_cancelled_keeps_parts(self, tmp_path: Path) -> None:
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        stop = threading.Event()
        stop.set()
        client = FakeHttpClient([ok_response(body)], retries=3)

        with pytest.raises(DownloadCancelled):
            AssetDownloader(client, stop_event=stop).download(asset)  # type: ignore[arg-type]

        raw_part, meta_part, _ = part_paths(asset)
        assert raw_part.exists()
        assert meta_part.exists()
        assert len(client.requests) == 1


# --------------------------------------------------------------------------
# Transform behaviour
# --------------------------------------------------------------------------


def _encrypt(plain: bytes, seed: int, label_crc: int, size: int) -> bytes:
    material = (
        seed.to_bytes(8, "big") + label_crc.to_bytes(8, "big") + size.to_bytes(8, "big")
    )
    digest = hashlib.sha1(material).digest()
    encryptor = Cipher(algorithms.AES(digest[:16]), modes.CTR(digest[4:20])).encryptor()
    return encryptor.update(plain) + encryptor.finalize()


class TestTransform:
    """Tests for each supported resource type."""

    def test_bundle_plain(self, tmp_path: Path) -> None:
        body = b"UnityFS-plain"
        asset = make_asset(
            tmp_path, make_entry(len(body), resource_type=ResourceType.BundlePlain)
        )
        client = FakeHttpClient([ok_response(body)])

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert Path(asset.output_path).read_bytes() == body
        assert result.size == len(body)

    def test_raw_plain(self, tmp_path: Path) -> None:
        body = b"raw"
        asset = make_asset(
            tmp_path, make_entry(len(body), resource_type=ResourceType.RawPlain)
        )
        client = FakeHttpClient([ok_response(body)])

        AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert Path(asset.output_path).read_bytes() == body

    def test_bundle_padded_start(self, tmp_path: Path) -> None:
        payload = b"UnityFS-padded"
        body = PADDED_BUNDLE_PREFIX + payload
        asset = make_asset(
            tmp_path,
            make_entry(len(body), resource_type=ResourceType.BundlePaddedStart),
        )
        client = FakeHttpClient([ok_response(body)])

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert Path(asset.output_path).read_bytes() == payload
        assert result.size == len(payload)

    def test_bundle_padded_start_bad_prefix(self, tmp_path: Path) -> None:
        body = b"XXXX" + b"payload"
        asset = make_asset(
            tmp_path,
            make_entry(len(body), resource_type=ResourceType.BundlePaddedStart),
        )
        client = FakeHttpClient([ok_response(body) for _ in range(4)], retries=3)

        with pytest.raises(DownloadError) as excinfo:
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert isinstance(excinfo.value.__cause__, TransformError)
        assert not Path(asset.output_path).exists()

    def test_raw_lz4(self, tmp_path: Path) -> None:
        plain = b"lz4 payload " * 100
        body = lz4.frame.compress(plain)
        asset = make_asset(
            tmp_path,
            make_entry(len(body), resource_type=ResourceType.RawLz4Compressed),
        )
        client = FakeHttpClient([ok_response(body)])

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert Path(asset.output_path).read_bytes() == plain
        assert result.size == len(plain)

    def test_raw_encrypted(self, tmp_path: Path) -> None:
        plain = b"encrypted payload " * 50
        compressed = lz4.frame.compress(plain)
        seed, label_crc = 7, 12345
        body = _encrypt(compressed, seed, label_crc, len(compressed))
        asset = make_asset(
            tmp_path,
            make_entry(
                len(body),
                resource_type=ResourceType.RawEncrypted,
                seed=seed,
                label_crc=label_crc,
            ),
        )
        client = FakeHttpClient([ok_response(body)])

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert Path(asset.output_path).read_bytes() == plain
        assert result.size == len(plain)

    def test_transform_failure_keeps_existing_target(self, tmp_path: Path) -> None:
        body = b"not-a-valid-lz4-frame"
        asset = make_asset(
            tmp_path,
            make_entry(len(body), resource_type=ResourceType.RawLz4Compressed),
        )
        output = Path(asset.output_path)
        output.parent.mkdir(parents=True)
        output.write_bytes(b"previous content")
        client = FakeHttpClient([ok_response(body) for _ in range(4)], retries=3)

        with pytest.raises(DownloadError):
            AssetDownloader(client, force=True).download(asset)  # type: ignore[arg-type]

        assert output.read_bytes() == b"previous content"

    def test_unsupported_resource_type(self, tmp_path: Path) -> None:
        body = b"data"
        entry = CatalogEntry(
            label="file",
            type_name="asset",
            size=len(body),
            checksum=0,
            seed=0,
            label_crc=0,
            priority=0,
            resource_type=None,
            resource_type_value=999,
        )
        asset = make_asset(tmp_path, entry)
        client = FakeHttpClient([ok_response(body)], retries=3)

        with pytest.raises(Exception) as excinfo:
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert type(excinfo.value).__name__ == "UnsupportedResourceType"
        assert not Path(asset.output_path).exists()


# --------------------------------------------------------------------------
# Atomic replace / metadata write failures
# --------------------------------------------------------------------------


class TestAtomicity:
    """Tests for atomic replace and metadata write failures."""

    def test_replace_failure_does_not_report_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = b"payload"
        asset = make_asset(tmp_path, make_entry(len(body)))
        client = FakeHttpClient([ok_response(body) for _ in range(4)], retries=3)

        real_replace = Path.replace

        def failing_replace(self: Path, target: Any) -> Path:
            raise OSError("replace failed")

        monkeypatch.setattr(Path, "replace", failing_replace)

        with pytest.raises(OSError):
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "replace", real_replace)
        assert not Path(asset.output_path).exists()

    def test_metadata_write_failure_aborts_download(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = b"payload"
        asset = make_asset(tmp_path, make_entry(len(body)))
        client = FakeHttpClient([ok_response(body) for _ in range(4)], retries=3)

        def failing_write(path: Path, value: Any) -> None:
            raise OSError("metadata write failed")

        monkeypatch.setattr("prism_sync.downloader.write_json_atomic", failing_write)

        with pytest.raises(OSError):
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert not Path(asset.output_path).exists()


# --------------------------------------------------------------------------
# previous_entries influence
# --------------------------------------------------------------------------


class TestPreviousEntries:
    """Tests for how prior state affects existing-file validity."""

    def test_recorded_real_name_mismatch_forces_redownload(
        self, tmp_path: Path
    ) -> None:
        body = b"abc"
        asset = make_asset(tmp_path, make_entry(len(body)))
        output = Path(asset.output_path)
        output.parent.mkdir(parents=True)
        output.write_bytes(body)
        client = FakeHttpClient([ok_response(body)])
        previous = {
            asset.relative_path: {
                "realName": "other",
                "size": len(body),
                "status": "downloaded",
            }
        }

        result = AssetDownloader(client, previous_entries=previous).download(  # type: ignore[arg-type]
            asset
        )

        assert result.status == "downloaded"
        assert len(client.requests) == 1

    def test_recorded_failed_status_forces_redownload(self, tmp_path: Path) -> None:
        body = b"abc"
        asset = make_asset(tmp_path, make_entry(len(body)))
        output = Path(asset.output_path)
        output.parent.mkdir(parents=True)
        output.write_bytes(body)
        client = FakeHttpClient([ok_response(body)])
        previous = {
            asset.relative_path: {
                "realName": asset.real_name,
                "size": len(body),
                "status": "failed",
            }
        }

        result = AssetDownloader(client, previous_entries=previous).download(  # type: ignore[arg-type]
            asset
        )

        assert result.status == "downloaded"

    def test_recorded_match_skips_download(self, tmp_path: Path) -> None:
        body = b"abc"
        asset = make_asset(tmp_path, make_entry(len(body)))
        output = Path(asset.output_path)
        output.parent.mkdir(parents=True)
        output.write_bytes(body)
        sha = hashlib.sha256(body).hexdigest()
        client = FakeHttpClient([])
        previous = {
            asset.relative_path: {
                "realName": asset.real_name,
                "size": len(body),
                "status": "downloaded",
                "sha256": sha,
            }
        }

        result = AssetDownloader(client, previous_entries=previous).download(  # type: ignore[arg-type]
            asset
        )

        assert result.status == "skipped"
        assert result.sha256 == sha
        assert client.requests == []


# --------------------------------------------------------------------------
# DownloadResult shape
# --------------------------------------------------------------------------


def test_download_result_fields() -> None:
    """``DownloadResult`` keeps all eight original fields."""
    result = DownloadResult(
        status="downloaded",
        size=1,
        raw_size=2,
        verified=True,
        raw_md5="a",
        sha256="b",
        etag="c",
        resumed=True,
    )
    assert result.status == "downloaded"
    assert result.size == 1
    assert result.raw_size == 2
    assert result.verified is True
    assert result.raw_md5 == "a"
    assert result.sha256 == "b"
    assert result.etag == "c"
    assert result.resumed is True


def test_goog_hash_md5_is_verified(tmp_path: Path) -> None:
    """``x-goog-hash`` md5 takes precedence over the ETag."""
    body = b"payload"
    md5_b64 = base64.b64encode(
        hashlib.md5(body, usedforsecurity=False).digest()
    ).decode()
    asset = make_asset(tmp_path, make_entry(len(body)))
    client = FakeHttpClient(
        [FakeResponse(200, body, {"x-goog-hash": f"md5={md5_b64}", "etag": "nope"})]
    )

    result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

    assert result.raw_md5 == hashlib.md5(body, usedforsecurity=False).hexdigest()


# --------------------------------------------------------------------------
# LZ4 multi-frame decoding
# --------------------------------------------------------------------------


class TestLz4MultiFrame:
    """Tests for ``_Lz4StreamDecoder`` handling concatenated frames."""

    def test_two_frames_in_one_feed(self) -> None:
        """Two independently compressed frames decode in order."""
        first = b"first-frame-payload" * 20
        second = b"second-frame-payload" * 30
        stream = lz4.frame.compress(first) + lz4.frame.compress(second)

        decoder = _Lz4StreamDecoder()
        output = decoder.feed(stream)
        decoder.finish()

        assert output == first + second

    def test_three_frames_split_across_feeds(self) -> None:
        """Frames arriving in arbitrary chunks still decode completely."""
        parts = [b"alpha" * 10, b"beta" * 20, b"gamma" * 30]
        stream = b"".join(lz4.frame.compress(part) for part in parts)

        decoder = _Lz4StreamDecoder()
        collected = bytearray()
        # Feed in 7-byte slices so frame boundaries land mid-chunk.
        for start in range(0, len(stream), 7):
            collected += decoder.feed(stream[start : start + 7])
        decoder.finish()

        assert bytes(collected) == b"".join(parts)

    def test_no_duplicate_output(self) -> None:
        """Each plaintext byte appears exactly once across frame boundaries."""
        first = b"A" * 64
        second = b"B" * 64
        stream = lz4.frame.compress(first) + lz4.frame.compress(second)

        decoder = _Lz4StreamDecoder()
        output = decoder.feed(stream)
        decoder.finish()

        assert output.count(b"A") == 64
        assert output.count(b"B") == 64
        assert len(output) == 128

    def test_single_frame_still_works(self) -> None:
        """The single-frame path is unaffected by multi-frame support."""
        payload = b"single" * 50
        decoder = _Lz4StreamDecoder()
        output = decoder.feed(lz4.frame.compress(payload))
        decoder.finish()

        assert output == payload

    def test_truncated_stream_raises(self) -> None:
        """A stream cut mid-frame fails in ``finish``."""
        stream = lz4.frame.compress(b"payload" * 100)
        decoder = _Lz4StreamDecoder()
        decoder.feed(stream[: len(stream) // 2])

        with pytest.raises(ValueError, match="truncated LZ4 frame"):
            decoder.finish()

    def test_empty_stream_raises(self) -> None:
        """No frames at all is treated as truncated."""
        decoder = _Lz4StreamDecoder()
        decoder.feed(b"")

        with pytest.raises(ValueError, match="truncated LZ4 frame"):
            decoder.finish()

    def test_multi_frame_through_downloader(self, tmp_path: Path) -> None:
        """The downloader decodes a multi-frame object end to end."""
        first = b"chunk-one-" * 40
        second = b"chunk-two-" * 40
        body = lz4.frame.compress(first) + lz4.frame.compress(second)
        asset = make_asset(
            tmp_path,
            make_entry(len(body), resource_type=ResourceType.RawLz4Compressed),
        )
        client = FakeHttpClient([ok_response(body)])

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert Path(asset.output_path).read_bytes() == first + second
        assert result.size == len(first) + len(second)


# --------------------------------------------------------------------------
# Metadata flush thresholds
# --------------------------------------------------------------------------


class TestMetadataFlush:
    """Tests for the 8 MB / 2 s metadata flush thresholds."""

    @staticmethod
    def _install_clock(
        monkeypatch: pytest.MonkeyPatch, ticks: list[float]
    ) -> list[float]:
        """Replace ``time.monotonic`` with a scripted clock."""
        remaining = list(ticks)
        fallback = ticks[-1] if ticks else 0.0

        def fake_monotonic() -> float:
            return remaining.pop(0) if remaining else fallback

        monkeypatch.setattr("prism_sync.downloader.time.monotonic", fake_monotonic)
        return remaining

    @staticmethod
    def _capture_meta_writes(
        monkeypatch: pytest.MonkeyPatch,
    ) -> list[dict[str, Any]]:
        """Record every metadata document written during a download."""
        writes: list[dict[str, Any]] = []
        real_write = write_json_atomic

        def recording_write(path: Path, value: Any) -> None:
            writes.append(dict(value))
            real_write(path, value)

        monkeypatch.setattr("prism_sync.downloader.write_json_atomic", recording_write)
        return writes

    def test_bytes_threshold_triggers_flush(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Crossing the byte threshold writes metadata mid-download."""
        monkeypatch.setattr("prism_sync.downloader._META_FLUSH_BYTES", 1024)
        # Clock never advances, so only the byte threshold can fire.
        self._install_clock(monkeypatch, [0.0] * 64)
        writes = self._capture_meta_writes(monkeypatch)

        body = b"x" * 4096
        asset = make_asset(tmp_path, make_entry(len(body)))
        client = FakeHttpClient([ok_response(body)], chunk_size=1024)

        AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        mid_download = [w for w in writes if w["downloaded"] not in (0, len(body))]
        assert mid_download, "expected at least one mid-download metadata write"
        assert all(w["downloaded"] % 1024 == 0 for w in mid_download)

    def test_time_threshold_triggers_flush(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Elapsed time alone triggers a flush even below the byte threshold."""
        monkeypatch.setattr("prism_sync.downloader._META_FLUSH_BYTES", 1 << 30)
        # First call sets last_flush; the next chunk's call is 5 s later.
        self._install_clock(monkeypatch, [0.0, 5.0, 5.0, 5.0, 5.0])
        writes = self._capture_meta_writes(monkeypatch)

        body = b"y" * 10
        asset = make_asset(tmp_path, make_entry(len(body)))
        client = FakeHttpClient([ok_response(body)])

        AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        # Initial write (downloaded=0), one time-triggered write, final write.
        assert len(writes) >= 3
        assert writes[0]["downloaded"] == 0
        assert writes[-1]["downloaded"] == len(body)

    def test_no_flush_below_both_thresholds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A small, fast download writes only the initial and final metadata."""
        monkeypatch.setattr("prism_sync.downloader._META_FLUSH_BYTES", 1 << 30)
        self._install_clock(monkeypatch, [0.0] * 64)
        writes = self._capture_meta_writes(monkeypatch)

        body = b"z" * 32
        asset = make_asset(tmp_path, make_entry(len(body)))
        client = FakeHttpClient([ok_response(body)])

        AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert [w["downloaded"] for w in writes] == [0, len(body)]

    def test_final_metadata_reflects_full_size(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The last metadata document records the complete byte count."""
        monkeypatch.setattr("prism_sync.downloader._META_FLUSH_BYTES", 512)
        self._install_clock(monkeypatch, [0.0] * 128)
        writes = self._capture_meta_writes(monkeypatch)

        body = b"w" * 2048
        asset = make_asset(tmp_path, make_entry(len(body)))
        client = FakeHttpClient([ok_response(body)], chunk_size=512)

        AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert writes[-1]["downloaded"] == len(body)
        assert writes[-1]["rawSize"] == len(body)
        assert writes[-1]["realName"] == asset.real_name

    def test_flush_write_failure_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing mid-download flush surfaces as an error, not a success."""
        monkeypatch.setattr("prism_sync.downloader._META_FLUSH_BYTES", 256)
        self._install_clock(monkeypatch, [0.0] * 128)

        calls = {"count": 0}
        real_write = write_json_atomic

        def failing_after_first(path: Path, value: Any) -> None:
            calls["count"] += 1
            if calls["count"] > 1:
                raise OSError("flush failed")
            real_write(path, value)

        monkeypatch.setattr(
            "prism_sync.downloader.write_json_atomic", failing_after_first
        )

        body = b"v" * 4096
        asset = make_asset(tmp_path, make_entry(len(body)))
        client = FakeHttpClient(
            [ok_response(body) for _ in range(4)], retries=3, chunk_size=256
        )

        with pytest.raises(OSError, match="flush failed"):
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert not Path(asset.output_path).exists()


# --------------------------------------------------------------------------
# Resume with a response that omits ETag
# --------------------------------------------------------------------------


class TestResumeWithoutResponseEtag:
    """Tests for resuming when the server sends no ``ETag`` header."""

    def _seed(self, asset: CatalogAsset, raw: bytes, etag: str) -> None:
        raw_part, meta_part, _ = part_paths(asset)
        raw_part.parent.mkdir(parents=True)
        raw_part.write_bytes(raw)
        meta_part.write_text(
            json.dumps(
                {
                    "realName": asset.real_name,
                    "rawSize": asset.entry.size,
                    "downloaded": len(raw),
                    "etag": etag,
                    "lastModified": "Wed, 21 Oct 2015 07:28:00 GMT",
                    "rawMd5": None,
                }
            ),
            encoding="utf-8",
        )

    def test_missing_response_etag_does_not_fail_resume(self, tmp_path: Path) -> None:
        """A stored ETag with no response ETag is not treated as a change."""
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed(asset, body[:4], "stored-etag")
        # 206 response without any ETag header.
        response = FakeResponse(
            206,
            body[4:],
            {"content-range": f"bytes 4-9/{len(body)}"},
        )
        client = FakeHttpClient([response])

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert result.resumed is True
        assert client.requests[0]["headers"]["Range"] == "bytes=4-"
        assert "If-Range" not in client.requests[0]["headers"]
        assert Path(asset.output_path).read_bytes() == body

    def test_missing_response_etag_writes_none_into_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Metadata records ``None`` for the ETag when the response omits it."""
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed(asset, body[:4], "stored-etag")

        writes: list[dict[str, Any]] = []
        real_write = write_json_atomic

        def recording_write(path: Path, value: Any) -> None:
            writes.append(dict(value))
            real_write(path, value)

        monkeypatch.setattr("prism_sync.downloader.write_json_atomic", recording_write)

        response = FakeResponse(
            206,
            body[4:],
            {"content-range": f"bytes 4-9/{len(body)}"},
        )
        client = FakeHttpClient([response])

        AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        # The response ETag is absent, so the freshly written metadata carries
        # ``None``; the stored value is only reused for MD5 verification.
        assert writes[0]["etag"] is None
        assert Path(asset.output_path).read_bytes() == body

    def test_etag_change_still_detected_when_both_present(self, tmp_path: Path) -> None:
        """Regression guard: a real ETag change is still an error."""
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed(asset, body[:4], "old-etag")
        client = FakeHttpClient(
            [
                FakeResponse(
                    206,
                    body[4:],
                    {
                        "content-range": f"bytes 4-9/{len(body)}",
                        "etag": "new-etag",
                    },
                )
                for _ in range(4)
            ],
            retries=3,
        )

        with pytest.raises(DownloadError) as excinfo:
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert isinstance(excinfo.value.__cause__, RangeNotSatisfiableError)
