"""Tests for the HTTP client module."""

from __future__ import annotations

import pytest

from prism_sync.http_client import (
    BytesResponse,
    DownloadError,
    HttpClient,
    NotFoundError,
    RangeNotSatisfiableError,
    RetryableHTTPError,
    build_asset_url,
)


class TestBuildAssetUrl:
    """Tests for build_asset_url function."""

    def test_valid_url(self) -> None:
        """Test building a valid asset URL."""
        result = build_asset_url("abcdef1234567890", "https://example.com")
        assert result == "https://example.com/r/ab/abcdef1234567890"

    def test_url_with_trailing_slash(self) -> None:
        """Test that trailing slash is stripped from base URL."""
        result = build_asset_url("abcdef", "https://example.com/")
        assert result == "https://example.com/r/ab/abcdef"

    def test_short_real_name_raises(self) -> None:
        """Test that real name shorter than 2 chars raises ValueError."""
        with pytest.raises(ValueError, match="real name is too short"):
            build_asset_url("a", "https://example.com")

    def test_empty_real_name_raises(self) -> None:
        """Test that empty real name raises ValueError."""
        with pytest.raises(ValueError, match="real name is too short"):
            build_asset_url("", "https://example.com")

    def test_default_base_url(self) -> None:
        """Test using default base URL."""
        result = build_asset_url("abcdef1234567890")
        assert result.startswith("https://asset.imassc.song4.prism.bn765.com/r/ab/")


class TestHttpClientInit:
    """Tests for HttpClient initialization."""

    def test_default_init(self) -> None:
        """Test default initialization."""
        client = HttpClient()
        assert client.base_url == "https://asset.imassc.song4.prism.bn765.com"
        assert client.retries == 3
        assert client.backoff == 0.5
        client.close()

    def test_custom_base_url(self) -> None:
        """Test custom base URL."""
        client = HttpClient(base_url="https://custom.example.com/")
        assert client.base_url == "https://custom.example.com"
        client.close()

    def test_custom_timeouts(self) -> None:
        """Test custom timeout values."""
        client = HttpClient(connect_timeout=5.0, read_timeout=30.0)
        assert client.base_url == "https://asset.imassc.song4.prism.bn765.com"
        client.close()

    def test_negative_retries_raises(self) -> None:
        """Test that negative retries raises ValueError."""
        with pytest.raises(ValueError, match="retries must be non-negative"):
            HttpClient(retries=-1)

    def test_context_manager(self) -> None:
        """Test context manager support."""
        with HttpClient() as client:
            assert client.base_url == "https://asset.imassc.song4.prism.bn765.com"


class TestBytesResponse:
    """Tests for BytesResponse dataclass."""

    def test_creation(self) -> None:
        """Test BytesResponse creation."""
        import httpx

        response = BytesResponse(
            content=b"test content",
            status_code=200,
            headers=httpx.Headers({"Content-Type": "application/octet-stream"}),
        )
        assert response.content == b"test content"
        assert response.status_code == 200
        assert "Content-Type" in response.headers

    def test_none_content(self) -> None:
        """Test BytesResponse with None content (304 Not Modified)."""
        import httpx

        response = BytesResponse(
            content=None,
            status_code=304,
            headers=httpx.Headers({}),
        )
        assert response.content is None
        assert response.status_code == 304


class TestExceptionHierarchy:
    """Tests for exception class hierarchy."""

    def test_download_error_is_runtime_error(self) -> None:
        """Test that DownloadError inherits from RuntimeError."""
        assert issubclass(DownloadError, RuntimeError)

    def test_not_found_error_is_download_error(self) -> None:
        """Test that NotFoundError inherits from DownloadError."""
        assert issubclass(NotFoundError, DownloadError)

    def test_retryable_http_error_is_download_error(self) -> None:
        """Test that RetryableHTTPError inherits from DownloadError."""
        assert issubclass(RetryableHTTPError, DownloadError)

    def test_range_not_satisfiable_error_is_download_error(self) -> None:
        """Test that RangeNotSatisfiableError inherits from DownloadError."""
        assert issubclass(RangeNotSatisfiableError, DownloadError)

    def test_retryable_http_error_attributes(self) -> None:
        """Test RetryableHTTPError attributes."""
        exc = RetryableHTTPError(503, retry_after=5.0)
        assert exc.status_code == 503
        assert exc.retry_after == 5.0
        assert "503" in str(exc)

    def test_range_not_satisfiable_error_message(self) -> None:
        """Test RangeNotSatisfiableError message."""
        exc = RangeNotSatisfiableError()
        assert "not satisfiable" in str(exc)

    def test_range_not_satisfiable_error_custom_message(self) -> None:
        """Test RangeNotSatisfiableError with custom message."""
        exc = RangeNotSatisfiableError("custom message")
        assert str(exc) == "custom message"
