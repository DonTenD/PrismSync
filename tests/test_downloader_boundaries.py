"""Boundary tests for the downloader left uncovered by ``test_downloader``.

Each test drives the real :class:`~prism_sync.downloader.AssetDownloader`
against a scripted HTTP client and asserts on the bytes written to disk.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any

import pytest

from prism_sync.downloader import (
    PART_META_SUFFIX,
    PART_RAW_SUFFIX,
    PART_TMP_SUFFIX,
    AssetDownloader,
    DownloadResult,
    Md5MismatchError,
    SizeMismatchError,
    download_assets,
)
from prism_sync.errors import DownloadCancelled
from prism_sync.http_client import (
    DownloadError,
    RangeNotSatisfiableError,
    RetryableHTTPError,
)
from prism_sync.models import CatalogAsset, ResourceType
from tests.test_downloader import (
    FakeHttpClient,
    FakeResponse,
    make_asset,
    make_entry,
    ok_response,
    part_paths,
    partial_response,
)

# --------------------------------------------------------------------------
# Local helpers
# --------------------------------------------------------------------------


def _lz4_body(plain: bytes) -> bytes:
    """Return ``plain`` compressed as a single LZ4 frame (stored form)."""
    import lz4.frame

    compressed: bytes = lz4.frame.compress(plain)
    return compressed


def _lz4_asset(
    tmp_path: Path, plain: bytes, name: str = "packed.bin"
) -> tuple[CatalogAsset, bytes]:
    """Build an ``RawLz4Compressed`` asset plus the bytes stored on the CDN."""
    body = _lz4_body(plain)
    entry = make_entry(len(body), resource_type=ResourceType.RawLz4Compressed)
    asset = make_asset(tmp_path, entry, name=name)
    return asset, body


def _parts(asset: CatalogAsset) -> tuple[Path, Path, Path]:
    return part_paths(asset)


# --------------------------------------------------------------------------
# verify=True semantics
# --------------------------------------------------------------------------


class TestVerifyBehaviour:
    """``verify`` only affects reuse of files whose size is not derivable."""

    @pytest.mark.parametrize("verify", [True, False])
    def test_plain_asset_downloads_with_either_flag(
        self, tmp_path: Path, verify: bool
    ) -> None:
        """A ``RawPlain`` asset transfers regardless of ``verify``."""
        body = b"plain-payload"
        asset = make_asset(tmp_path, make_entry(len(body)))
        client = FakeHttpClient([ok_response(body)])

        result = AssetDownloader(client, verify=verify).download(asset)  # type: ignore[arg-type]

        assert result.status == "downloaded"
        assert Path(asset.output_path).read_bytes() == body

    def test_plain_asset_reused_with_verify_true(self, tmp_path: Path) -> None:
        """Derivable sizes stay reusable even with ``verify=True``."""
        body = b"cached-plain"
        asset = make_asset(tmp_path, make_entry(len(body)))
        output = Path(asset.output_path)
        output.parent.mkdir(parents=True)
        output.write_bytes(body)
        client = FakeHttpClient([])

        result = AssetDownloader(client, verify=True).download(asset)  # type: ignore[arg-type]

        assert result.status == "skipped"
        assert client.requests == []

    def test_md5_mismatch_fails_even_without_verify(self, tmp_path: Path) -> None:
        """MD5 verification is independent of the ``verify`` flag."""
        body = b"payload"
        asset = make_asset(tmp_path, make_entry(len(body)))
        wrong_etag = hashlib.md5(b"other", usedforsecurity=False).hexdigest()
        bad = FakeResponse(200, body, {"etag": wrong_etag})
        client = FakeHttpClient([bad for _ in range(4)], retries=3)

        with pytest.raises(DownloadError) as excinfo:
            AssetDownloader(client, verify=False).download(asset)  # type: ignore[arg-type]

        assert isinstance(excinfo.value.__cause__, Md5MismatchError)
        assert not Path(asset.output_path).exists()

    def test_md5_present_passes_with_verify_true(self, tmp_path: Path) -> None:
        """A correct ETag passes when ``verify=True``."""
        body = b"verify-ok"
        asset = make_asset(tmp_path, make_entry(len(body)))
        client = FakeHttpClient([ok_response(body)])

        result = AssetDownloader(client, verify=True).download(asset)  # type: ignore[arg-type]

        assert result.verified is True
        assert result.raw_md5 == hashlib.md5(body, usedforsecurity=False).hexdigest()

    def test_md5_absent_still_downloads(self, tmp_path: Path) -> None:
        """With no MD5 header at all only the size is checked."""
        body = b"no-md5"
        asset = make_asset(tmp_path, make_entry(len(body)))
        client = FakeHttpClient([FakeResponse(200, body)])

        result = AssetDownloader(client, verify=True).download(asset)  # type: ignore[arg-type]

        assert result.status == "downloaded"
        assert Path(asset.output_path).read_bytes() == body


class TestVerifyWithLz4:
    """``RawLz4Compressed`` output size is unknowable, so ``verify`` matters."""

    def test_first_download(self, tmp_path: Path) -> None:
        """``verify=True`` still downloads a fresh LZ4 asset correctly."""
        plain = b"lz4 verify payload " * 40
        asset, body = _lz4_asset(tmp_path, plain)
        client = FakeHttpClient([ok_response(body)])

        result = AssetDownloader(client, verify=True).download(asset)  # type: ignore[arg-type]

        assert result.status == "downloaded"
        assert Path(asset.output_path).read_bytes() == plain
        assert result.raw_size == len(body)

    def test_existing_file_redownloaded_with_verify(self, tmp_path: Path) -> None:
        """A pre-existing LZ4 output is not trusted when ``verify=True``."""
        plain = b"lz4 existing " * 60
        asset, body = _lz4_asset(tmp_path, plain)
        output = Path(asset.output_path)
        output.parent.mkdir(parents=True)
        output.write_bytes(b"stale-content")
        client = FakeHttpClient([ok_response(body)])

        result = AssetDownloader(client, verify=True).download(asset)  # type: ignore[arg-type]

        assert result.status == "downloaded"
        assert client.requests != []
        assert output.read_bytes() == plain

    def test_existing_file_reused_without_verify(self, tmp_path: Path) -> None:
        """A non-empty LZ4 output is reused when ``verify=False``."""
        plain = b"lz4 reused " * 60
        asset, _body = _lz4_asset(tmp_path, plain)
        output = Path(asset.output_path)
        output.parent.mkdir(parents=True)
        output.write_bytes(b"previous-lz4-output")
        client = FakeHttpClient([])

        result = AssetDownloader(client, verify=False).download(asset)  # type: ignore[arg-type]

        assert result.status == "skipped"
        assert client.requests == []
        assert output.read_bytes() == b"previous-lz4-output"

    def test_md5_still_checked_with_verify(self, tmp_path: Path) -> None:
        """``verify`` never disables the raw-bytes MD5 check."""
        plain = b"lz4 md5 " * 30
        _asset, body = _lz4_asset(tmp_path, plain)
        bad = FakeResponse(
            200, body, {"etag": hashlib.md5(b"nope", usedforsecurity=False).hexdigest()}
        )
        client = FakeHttpClient([bad for _ in range(4)], retries=3)

        with pytest.raises(DownloadError) as excinfo:
            AssetDownloader(client, verify=True).download(_asset)  # type: ignore[arg-type]

        assert isinstance(excinfo.value.__cause__, Md5MismatchError)

    def test_empty_existing_file_redownloads_without_verify(
        self, tmp_path: Path
    ) -> None:
        """A zero-length LZ4 output is never reused."""
        plain = b"lz4 empty " * 20
        asset, body = _lz4_asset(tmp_path, plain)
        output = Path(asset.output_path)
        output.parent.mkdir(parents=True)
        output.write_bytes(b"")
        client = FakeHttpClient([ok_response(body)])

        result = AssetDownloader(client, verify=False).download(asset)  # type: ignore[arg-type]

        assert result.status == "downloaded"
        assert output.read_bytes() == plain


# --------------------------------------------------------------------------
# Real AssetDownloader with jobs > 1
# --------------------------------------------------------------------------


class _PerKeyHttpClient:
    """Thread-safe fake client returning one scripted response per URL."""

    def __init__(
        self,
        responses: dict[str, list[Any]],
        *,
        retries: int = 0,
        base_url: str = "https://cdn.example",
    ) -> None:
        self.responses = {k: list(v) for k, v in responses.items()}
        self.retries = retries
        self.base_url = base_url
        self.lock = threading.Lock()
        self.calls: list[str] = []

    def stream_response(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        chunk_size: int = 1 << 20,
    ) -> Any:
        from contextlib import contextmanager

        with self.lock:
            self.calls.append(url)
            queue = self.responses.get(url)
            if not queue:
                raise AssertionError(f"unexpected request for {url}")
            item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item

        @contextmanager
        def ctx() -> Any:
            yield item, item.iter_bytes(chunk_size)

        return ctx()

    def _sleep_before_retry(self, attempt: int, retry_after: float | None) -> None:
        return None


def _asset_with_real_name(
    tmp_path: Path, name: str, size: int, real_name: str
) -> CatalogAsset:
    entry = make_entry(size)
    base = make_asset(tmp_path, entry, name=name)
    return CatalogAsset(
        entry=entry,
        catalog_label="root",
        output_path=base.output_path,
        relative_path=base.relative_path,
        real_name=real_name,
    )


def _url_for(real_name: str, base: str = "https://cdn.example") -> str:
    return f"{base}/r/{real_name[:2]}/{real_name}"


class TestConcurrentRealDownloader:
    """``download_assets`` driving the real downloader concurrently."""

    @pytest.mark.parametrize("jobs", [1, 2, 3])
    def test_all_assets_complete(self, tmp_path: Path, jobs: int) -> None:
        """Every asset lands on disk with its own exact bytes."""
        count = 5
        payloads = {f"r{i:03d}": (f"payload-{i}-" * 20).encode() for i in range(count)}
        assets = [
            _asset_with_real_name(tmp_path, f"f{i}.bin", len(p), real)
            for i, (real, p) in enumerate(payloads.items())
        ]
        responses = {_url_for(real): [ok_response(p)] for real, p in payloads.items()}
        client = _PerKeyHttpClient(responses)

        results = download_assets(
            AssetDownloader(client, resume=False),  # type: ignore[arg-type]
            assets,
            jobs=jobs,
        )

        assert len(results) == count
        assert all(isinstance(r, DownloadResult) for r in results)
        for asset, real in zip(assets, payloads, strict=True):
            assert Path(asset.output_path).read_bytes() == payloads[real]
        # No temporary artefacts survive.
        assert not list(tmp_path.rglob(f"*{PART_RAW_SUFFIX}"))
        assert not list(tmp_path.rglob(f"*{PART_META_SUFFIX}"))
        assert not list(tmp_path.rglob(f"*{PART_TMP_SUFFIX}"))

    def test_one_failure_does_not_block_others(self, tmp_path: Path) -> None:
        """A single broken asset leaves the rest intact."""
        payloads = {
            "aa01": b"first-payload",
            "bb02": b"second-payload",
            "cc03": b"third-payload",
        }
        assets = [
            _asset_with_real_name(tmp_path, f"{real}.bin", len(p), real)
            for real, p in payloads.items()
        ]
        responses = {_url_for(r): [ok_response(p)] for r, p in payloads.items()}
        responses[_url_for("bb02")] = [FakeResponse(500, b"", {})]
        client = _PerKeyHttpClient(responses, retries=0)

        results = download_assets(
            AssetDownloader(client, resume=False),  # type: ignore[arg-type]
            assets,
            jobs=3,
        )

        assert isinstance(results[0], DownloadResult)
        assert isinstance(results[1], Exception)
        assert isinstance(results[2], DownloadResult)
        assert Path(assets[0].output_path).read_bytes() == payloads["aa01"]
        assert Path(assets[2].output_path).read_bytes() == payloads["cc03"]
        assert not Path(assets[1].output_path).exists()

    def test_multiple_simultaneous_failures(self, tmp_path: Path) -> None:
        """Every failing asset is reported without losing the successes."""
        payloads = {"11aa": b"ok-one", "22bb": b"bad-one", "33cc": b"bad-two"}
        assets = [
            _asset_with_real_name(tmp_path, f"{r}.bin", len(p), r)
            for r, p in payloads.items()
        ]
        responses = {
            _url_for("11aa"): [ok_response(payloads["11aa"])],
            _url_for("22bb"): [FakeResponse(500, b"", {})],
            _url_for("33cc"): [FakeResponse(404, b"", {})],
        }
        client = _PerKeyHttpClient(responses, retries=0)

        results = download_assets(
            AssetDownloader(client, resume=False),  # type: ignore[arg-type]
            assets,
            jobs=3,
        )

        assert isinstance(results[0], DownloadResult)
        assert isinstance(results[1], Exception)
        assert isinstance(results[2], Exception)

    def test_concurrent_lz4_transforms(self, tmp_path: Path) -> None:
        """Concurrent transform + atomic replace never mixes file contents."""
        plains = {f"lx{i}": (f"lz4-plain-{i}-" * 30).encode() for i in range(4)}
        assets: list[CatalogAsset] = []
        responses: dict[str, list[Any]] = {}
        for real, plain in plains.items():
            packed = _lz4_body(plain)
            entry = make_entry(len(packed), resource_type=ResourceType.RawLz4Compressed)
            base = make_asset(tmp_path, entry, name=f"{real}.bin")
            assets.append(
                CatalogAsset(
                    entry=entry,
                    catalog_label="root",
                    output_path=base.output_path,
                    relative_path=base.relative_path,
                    real_name=real,
                )
            )
            responses[_url_for(real)] = [ok_response(packed)]
        client = _PerKeyHttpClient(responses)

        results = download_assets(
            AssetDownloader(client, resume=False),  # type: ignore[arg-type]
            assets,
            jobs=4,
        )

        assert all(isinstance(r, DownloadResult) for r in results)
        for asset, real in zip(assets, plains, strict=True):
            assert Path(asset.output_path).read_bytes() == plains[real]
        assert not list(tmp_path.rglob(f"*{PART_TMP_SUFFIX}"))

    def test_progress_is_serial_and_monotonic(self, tmp_path: Path) -> None:
        """With ``jobs>1`` the callback still runs serially on one thread."""
        payloads = {f"pg{i:02d}": (f"progress-{i}-" * 10).encode() for i in range(6)}
        assets = [
            _asset_with_real_name(tmp_path, f"{r}.bin", len(p), r)
            for r, p in payloads.items()
        ]
        responses = {_url_for(r): [ok_response(p)] for r, p in payloads.items()}
        client = _PerKeyHttpClient(responses)

        events: list[tuple[int, int, int]] = []
        seen_threads: set[int] = set()

        def on_progress(asset: CatalogAsset, done: int, total: int) -> None:
            seen_threads.add(threading.get_ident())
            events.append((done, total, id(asset)))

        download_assets(
            AssetDownloader(client, resume=False),  # type: ignore[arg-type]
            assets,
            jobs=4,
            on_progress=on_progress,
        )

        assert [done for done, _, _ in events] == list(range(1, 7))
        assert {total for _, total, _ in events} == {6}
        assert len({asset_id for _, _, asset_id in events}) == 6
        assert len(seen_threads) == 1, "callback must stay on the scheduler thread"

    def test_no_worker_threads_leak(self, tmp_path: Path) -> None:
        """The call returns only after every worker has exited."""
        payloads = {f"th{i}": (f"thread-{i}-" * 10).encode() for i in range(4)}
        assets = [
            _asset_with_real_name(tmp_path, f"{r}.bin", len(p), r)
            for r, p in payloads.items()
        ]
        responses = {_url_for(r): [ok_response(p)] for r, p in payloads.items()}
        client = _PerKeyHttpClient(responses)
        before = threading.active_count()

        download_assets(
            AssetDownloader(client, resume=False),  # type: ignore[arg-type]
            assets,
            jobs=4,
        )

        assert threading.active_count() <= before


# --------------------------------------------------------------------------
# HTTP 416 recovery
# --------------------------------------------------------------------------


class TestHttp416Recovery:
    """A rejected ``Range`` must not corrupt the final file."""

    def test_full_recovery_after_416(self, tmp_path: Path) -> None:
        """The downloader restarts from zero and produces the right bytes."""
        body = b"0123456789abcdef"
        asset = make_asset(tmp_path, make_entry(len(body)))
        raw_part, meta_part, _ = _parts(asset)
        raw_part.parent.mkdir(parents=True)
        raw_part.write_bytes(body[:6])
        meta_part.write_text(
            f'{{"realName": "{asset.real_name}", '
            f'"rawSize": {len(body)}, "downloaded": 6}}',
            encoding="utf-8",
        )
        client = FakeHttpClient(
            [RangeNotSatisfiableError("416"), ok_response(body)], retries=3
        )

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert result.status == "downloaded"
        assert Path(asset.output_path).read_bytes() == body
        # The first attempt carried the Range header; the retry did not.
        assert client.requests[0]["headers"].get("Range") == "bytes=6-"
        assert "Range" not in client.requests[1]["headers"]

    def test_416_does_not_append_stale_and_new_bytes(self, tmp_path: Path) -> None:
        """Recovery replaces the partial file instead of appending to it."""
        body = b"AAAABBBBCCCC"
        asset = make_asset(tmp_path, make_entry(len(body)))
        raw_part, meta_part, _ = _parts(asset)
        raw_part.parent.mkdir(parents=True)
        raw_part.write_bytes(b"XXXX")
        meta_part.write_text(
            f'{{"realName": "{asset.real_name}", '
            f'"rawSize": {len(body)}, "downloaded": 4}}',
            encoding="utf-8",
        )
        client = FakeHttpClient(
            [RangeNotSatisfiableError("416"), ok_response(body)], retries=3
        )

        AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert Path(asset.output_path).read_bytes() == body
        assert b"XXXX" not in Path(asset.output_path).read_bytes()


# --------------------------------------------------------------------------
# Retry state
# --------------------------------------------------------------------------


class TestRetryState:
    """What survives, and what does not, across retries."""

    def test_retry_success_has_no_duplicate_bytes(self, tmp_path: Path) -> None:
        """A transport error then success still yields exactly the payload."""
        body = b"retry-payload-0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        client = FakeHttpClient([_transport_error(), ok_response(body)], retries=3)

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert result.status == "downloaded"
        assert Path(asset.output_path).read_bytes() == body

    def test_exhausted_transport_retries_keep_parts(self, tmp_path: Path) -> None:
        """Network-class exhaustion leaves the partial bytes resumable."""
        body = b"keep-me-please"
        asset = make_asset(tmp_path, make_entry(len(body)))
        raw_part, _meta_part, tmp_part = _parts(asset)
        raw_part.parent.mkdir(parents=True)
        raw_part.write_bytes(b"")
        client = FakeHttpClient([_transport_error() for _ in range(3)], retries=2)

        with pytest.raises(DownloadError):
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        # Nothing was written, so the part files keep whatever state they had.
        assert not Path(asset.output_path).exists()
        assert not tmp_part.exists()
        assert len(client.requests) == 3

    def test_exhausted_integrity_retries_clear_parts(self, tmp_path: Path) -> None:
        """Integrity failures discard the parts before giving up."""
        body = b"short"
        asset = make_asset(tmp_path, make_entry(len(body) + 10))
        raw_part, meta_part, tmp_part = _parts(asset)
        client = FakeHttpClient([ok_response(body) for _ in range(2)], retries=1)

        with pytest.raises(DownloadError) as excinfo:
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert isinstance(excinfo.value.__cause__, SizeMismatchError)
        assert not raw_part.exists()
        assert not meta_part.exists()
        assert not tmp_part.exists()

    def test_retryable_status_retries_then_succeeds(self, tmp_path: Path) -> None:
        """A 503 followed by a 200 completes without leftovers."""
        body = b"after-503"
        asset = make_asset(tmp_path, make_entry(len(body)))
        client = FakeHttpClient(
            [RetryableHTTPError(503, retry_after=0.0), ok_response(body)], retries=3
        )

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert result.status == "downloaded"
        assert Path(asset.output_path).read_bytes() == body
        assert len(client.requests) == 2


def _transport_error() -> Exception:
    import httpx

    return httpx.ConnectError("boom")


# --------------------------------------------------------------------------
# HTTP 206 boundaries
# --------------------------------------------------------------------------


class TestHttp206Boundaries:
    """Malformed ``206`` responses must never be accepted silently."""

    def _seed_partial(self, asset: CatalogAsset, raw: bytes) -> None:
        raw_part, meta_part, _ = _parts(asset)
        raw_part.parent.mkdir(parents=True, exist_ok=True)
        raw_part.write_bytes(raw)
        meta_part.write_text(
            f'{{"realName": "{asset.real_name}", '
            f'"rawSize": {asset.entry.size}, "downloaded": {len(raw)}}}',
            encoding="utf-8",
        )

    def test_empty_206_body_is_rejected(self, tmp_path: Path) -> None:
        """A 206 with no body cannot satisfy the declared size."""
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed_partial(asset, body[:4])
        empty = FakeResponse(
            206,
            b"",
            {"content-range": f"bytes 4-9/{len(body)}"},
        )
        client = FakeHttpClient([empty for _ in range(3)], retries=2)

        with pytest.raises(DownloadError) as excinfo:
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        # The first attempt rejects the short body with a size error and clears
        # the parts; the retry then restarts at offset 0 and rejects the still
        # wrong Content-Range.  Either guard is acceptable, a silent success is
        # not.
        assert isinstance(
            excinfo.value.__cause__, (SizeMismatchError, RangeNotSatisfiableError)
        )
        assert not Path(asset.output_path).exists()

    def test_empty_206_first_attempt_is_size_error(self, tmp_path: Path) -> None:
        """A single attempt pinpoints the empty body as a size mismatch."""
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed_partial(asset, body[:4])
        empty = FakeResponse(206, b"", {"content-range": f"bytes 4-9/{len(body)}"})
        client = FakeHttpClient([empty], retries=0)

        with pytest.raises(DownloadError) as excinfo:
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert isinstance(excinfo.value.__cause__, SizeMismatchError)
        assert not Path(asset.output_path).exists()

    def test_content_range_end_before_start_is_now_rejected(
        self, tmp_path: Path
    ) -> None:
        """Closed by stage three: an empty range is refused, not accepted.

        ``TestContentRangeLegality`` holds the full contract; this keeps the
        stage-two entry point pointing at the tightened behaviour.
        """
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed_partial(asset, body[:4])
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

    def test_declared_range_longer_than_body_fails(self, tmp_path: Path) -> None:
        """A range claiming more bytes than delivered fails the size check."""
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed_partial(asset, body[:4])
        response = FakeResponse(
            206,
            body[4:7],
            {"content-range": f"bytes 4-9/{len(body)}"},
        )
        client = FakeHttpClient([response for _ in range(3)], retries=2)

        with pytest.raises(DownloadError) as excinfo:
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert isinstance(
            excinfo.value.__cause__, (SizeMismatchError, RangeNotSatisfiableError)
        )
        assert not Path(asset.output_path).exists()

    def test_declared_range_longer_than_body_first_attempt(
        self, tmp_path: Path
    ) -> None:
        """A single attempt reports the short transfer as a size mismatch."""
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed_partial(asset, body[:4])
        response = FakeResponse(
            206, body[4:7], {"content-range": f"bytes 4-9/{len(body)}"}
        )
        client = FakeHttpClient([response], retries=0)

        with pytest.raises(DownloadError) as excinfo:
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert isinstance(excinfo.value.__cause__, SizeMismatchError)

    def test_wrong_total_is_rejected(self, tmp_path: Path) -> None:
        """A ``total`` that disagrees with the catalog is a size error."""
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed_partial(asset, body[:4])
        client = FakeHttpClient(
            [partial_response(body[4:], start=4, total=999) for _ in range(3)],
            retries=2,
        )

        with pytest.raises(DownloadError) as excinfo:
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert isinstance(
            excinfo.value.__cause__, (SizeMismatchError, RangeNotSatisfiableError)
        )

    def test_206_without_content_range_is_rejected(self, tmp_path: Path) -> None:
        """A 206 lacking ``Content-Range`` cannot be verified."""
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed_partial(asset, body[:4])
        client = FakeHttpClient(
            [FakeResponse(206, body[4:], {}) for _ in range(3)], retries=2
        )

        with pytest.raises(DownloadError) as excinfo:
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert isinstance(excinfo.value.__cause__, RangeNotSatisfiableError)

    def test_complete_partial_avoids_network(self, tmp_path: Path) -> None:
        """A partial already matching the size is finished without a request."""
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed_partial(asset, body)
        client = FakeHttpClient([])

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert result.resumed is True
        assert client.requests == []
        assert Path(asset.output_path).read_bytes() == body


# --------------------------------------------------------------------------
# Cancellation interaction with parts
# --------------------------------------------------------------------------


class TestCancelledPartsRetention:
    def test_cancelled_download_keeps_partial_state(self, tmp_path: Path) -> None:
        """Cancelling mid-download leaves resumable parts behind."""
        body = b"cancel-me" * 100
        asset = make_asset(tmp_path, make_entry(len(body)))
        stop = threading.Event()
        stop.set()
        client = FakeHttpClient([ok_response(body)])

        with pytest.raises(DownloadCancelled):
            AssetDownloader(client, stop_event=stop).download(asset)  # type: ignore[arg-type]

        raw_part, meta_part, _ = _parts(asset)
        assert raw_part.exists()
        assert meta_part.exists()
        assert not Path(asset.output_path).exists()


# --------------------------------------------------------------------------
# Result ordering contract under concurrency
# --------------------------------------------------------------------------


class TestConcurrentOrdering:
    def test_results_align_with_input_order(self, tmp_path: Path) -> None:
        """Results keep input order even when completion order differs."""
        payloads = {
            "ord1": b"one",
            "ord2": b"two",
            "ord3": b"three",
        }
        assets = [
            _asset_with_real_name(tmp_path, f"{r}.bin", len(p), r)
            for r, p in payloads.items()
        ]
        responses = {_url_for(r): [ok_response(p)] for r, p in payloads.items()}
        client = _PerKeyHttpClient(responses)

        results = download_assets(
            AssetDownloader(client, resume=False),  # type: ignore[arg-type]
            assets,
            jobs=3,
        )

        assert len(results) == 3
        for asset, real in zip(assets, payloads, strict=True):
            assert Path(asset.output_path).read_bytes() == payloads[real]


# --------------------------------------------------------------------------
# Third stage: Content-Range legality (strict validation)
# --------------------------------------------------------------------------


class TestContentRangeLegality:
    """``Content-Range`` must be internally consistent and match the body.

    These tests pin the stricter contract introduced in stage three: a ``206``
    whose declared range is impossible (``end < start``) or whose declared
    length disagrees with the bytes actually delivered is rejected instead of
    being appended to the partial file.
    """

    def _seed_partial(self, asset: CatalogAsset, raw: bytes) -> None:
        raw_part, meta_part, _ = _parts(asset)
        raw_part.parent.mkdir(parents=True, exist_ok=True)
        raw_part.write_bytes(raw)
        meta_part.write_text(
            f'{{"realName": "{asset.real_name}", '
            f'"rawSize": {asset.entry.size}, "downloaded": {len(raw)}}}',
            encoding="utf-8",
        )

    def test_end_before_start_is_rejected(self, tmp_path: Path) -> None:
        """``bytes N-(N-1)/total`` is impossible and must not be accepted."""
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed_partial(asset, body[:4])
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

    def test_declared_length_shorter_than_body_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """A body longer than the declared range is a protocol violation."""
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed_partial(asset, body[:4])
        # Declares 4-6 (3 bytes) but delivers 6 bytes.
        response = FakeResponse(
            206,
            body[4:],
            {"content-range": f"bytes 4-6/{len(body)}"},
        )
        client = FakeHttpClient([response], retries=0)

        with pytest.raises(DownloadError) as excinfo:
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert isinstance(
            excinfo.value.__cause__, (SizeMismatchError, RangeNotSatisfiableError)
        )
        assert not Path(asset.output_path).exists()

    def test_declared_length_longer_than_body_is_rejected(self, tmp_path: Path) -> None:
        """A body shorter than the declared range must not be padded silently."""
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed_partial(asset, body[:4])
        # Declares 4-9 (6 bytes) but delivers 3 bytes.
        response = FakeResponse(
            206,
            body[4:7],
            {"content-range": f"bytes 4-9/{len(body)}"},
        )
        client = FakeHttpClient([response], retries=0)

        with pytest.raises(DownloadError) as excinfo:
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert isinstance(
            excinfo.value.__cause__, (SizeMismatchError, RangeNotSatisfiableError)
        )
        assert not Path(asset.output_path).exists()

    def test_valid_206_still_succeeds(self, tmp_path: Path) -> None:
        """A well-formed range keeps working after the stricter checks."""
        body = b"0123456789abcdef"
        asset = make_asset(tmp_path, make_entry(len(body)))
        self._seed_partial(asset, body[:6])
        client = FakeHttpClient(
            [partial_response(body[6:], start=6, total=len(body), full_body=body)]
        )

        result = AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert result.status == "downloaded"
        assert result.resumed is True
        assert Path(asset.output_path).read_bytes() == body

    def test_invalid_range_does_not_corrupt_existing_target(
        self, tmp_path: Path
    ) -> None:
        """A prior good file survives a rejected range response."""
        body = b"0123456789"
        asset = make_asset(tmp_path, make_entry(len(body)))
        output = Path(asset.output_path)
        output.parent.mkdir(parents=True)
        output.write_bytes(b"previous-good-content")
        self._seed_partial(asset, body[:4])
        response = FakeResponse(
            206, body[4:], {"content-range": f"bytes 4-3/{len(body)}"}
        )
        client = FakeHttpClient([response], retries=0)

        with pytest.raises(DownloadError):
            AssetDownloader(client).download(asset)  # type: ignore[arg-type]

        assert output.read_bytes() == b"previous-good-content"
