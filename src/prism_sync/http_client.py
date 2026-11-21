"""HTTP client for the Prism CDN.

The client deliberately does not parse catalogs. It only knows how to build
asset URLs, retry transient failures and stream bytes.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Self

import httpx

from .constants import DEFAULT_BASE_URL

LOGGER = logging.getLogger(__name__)

_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class DownloadError(RuntimeError):
    """Base class for HTTP download failures."""


class NotFoundError(DownloadError):
    """The CDN returned 404 for an object."""


class RetryableHTTPError(DownloadError):
    """A transient HTTP status that may succeed on retry."""

    def __init__(self, status_code: int, retry_after: float | None = None) -> None:
        """Initialize with status code and optional retry-after."""
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(f"transient HTTP status {status_code}")


class RangeNotSatisfiableError(DownloadError):
    """The server rejected a ``Range`` request (HTTP 416)."""

    def __init__(
        self, message: str = "requested byte range is not satisfiable"
    ) -> None:
        """Initialize with error message."""
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class BytesResponse:
    """A small-object response, including headers and 304 support."""

    content: bytes | None
    status_code: int
    headers: httpx.Headers


def build_asset_url(real_name: str, base_url: str = DEFAULT_BASE_URL) -> str:
    """Build ``<base>/r/<first two chars>/<real_name>``."""
    if len(real_name) < 2:
        raise ValueError(f"real name is too short: {real_name!r}")
    return f"{base_url.rstrip('/')}/r/{real_name[:2]}/{real_name}"


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class HttpClient:
    """Small wrapper around :class:`httpx.Client`.

    Binary resources are requested with ``Accept-Encoding: identity`` so the
    bytes on disk are exactly the bytes the game expects.  ``httpx`` would
    otherwise transparently decompress a gzip transfer encoding.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        retries: int = 3,
        backoff: float = 0.5,
        jobs: int = 8,
        user_agent: str = "prism-catalog/0.1",
        trust_env: bool = True,
    ) -> None:
        """Initialize the HTTP client with configuration."""
        if retries < 0:
            raise ValueError("retries must be non-negative")
        self.base_url = base_url.rstrip("/")
        self.retries = retries
        self.backoff = backoff
        self._client = httpx.Client(
            follow_redirects=True,
            trust_env=trust_env,
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=read_timeout,
                pool=connect_timeout,
            ),
            limits=httpx.Limits(
                max_connections=max(4, jobs * 2),
                max_keepalive_connections=max(2, jobs),
            ),
            headers={
                "Accept-Encoding": "identity",
                "User-Agent": user_agent,
            },
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> Self:
        """Enter context manager."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Exit context manager."""
        self.close()

    def _sleep_before_retry(self, attempt: int, retry_after: float | None) -> None:
        delay = retry_after if retry_after is not None else self.backoff * (2**attempt)
        delay += random.uniform(0.0, 0.25)
        delay = min(delay, 30.0)
        LOGGER.debug("retrying in %.2fs (attempt %d)", delay, attempt + 1)
        time.sleep(delay)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if status == 404:
            raise NotFoundError(f"404 Not Found: {response.request.url}")
        if status == 416:
            raise RangeNotSatisfiableError(
                f"416 Range Not Satisfiable: {response.request.url}"
            )
        if status in _RETRYABLE_STATUS:
            raise RetryableHTTPError(status, _retry_after_seconds(response))
        if status >= 400:
            raise DownloadError(f"HTTP {status}: {response.request.url}")

    def get_bytes_response(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        retries: int | None = None,
    ) -> BytesResponse:
        """Download a small object and return its headers.

        ``content`` is ``None`` for a ``304 Not Modified`` response.
        """
        attempts = self.retries if retries is None else retries
        last_error: Exception | None = None

        for attempt in range(attempts + 1):
            try:
                with self._client.stream("GET", url, headers=headers) as response:
                    self._raise_for_status(response)
                    if response.status_code == 304:
                        return BytesResponse(None, 304, response.headers)
                    return BytesResponse(
                        response.read(),
                        response.status_code,
                        response.headers,
                    )
            except RetryableHTTPError as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                self._sleep_before_retry(attempt, exc.retry_after)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                self._sleep_before_retry(attempt, None)

        assert last_error is not None
        raise DownloadError(f"failed to download {url}: {last_error}") from last_error

    def get_bytes(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        retries: int | None = None,
    ) -> bytes:
        """Download a small object (catalog, version JSON) into memory."""
        result = self.get_bytes_response(
            url,
            headers=headers,
            retries=retries,
        )
        if result.content is None:
            raise DownloadError(f"unexpected HTTP 304 for {url}")
        return result.content

    @contextmanager
    def stream_response(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        chunk_size: int = 1 << 20,
    ) -> Iterator[tuple[httpx.Response, Iterator[bytes]]]:
        """Yield the response object and an iterator of response chunks.

        The caller is responsible for retrying the whole operation if the
        stream fails mid-transfer.  Status-code retries are handled here only
        for the initial response.  ``headers`` is used for ``Range`` and
        conditional requests.
        """
        request = self._client.build_request("GET", url, headers=headers)
        response = self._client.send(request, stream=True)
        try:
            self._raise_for_status(response)
            yield response, response.iter_bytes(chunk_size)
        finally:
            response.close()

    @contextmanager
    def stream(
        self, url: str, *, chunk_size: int = 1 << 20
    ) -> Iterator[Iterator[bytes]]:
        """Yield an iterator of response chunks (compatibility helper)."""
        with self.stream_response(url, chunk_size=chunk_size) as (_, chunks):
            yield chunks
