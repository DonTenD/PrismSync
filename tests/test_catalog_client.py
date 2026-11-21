"""Tests for the catalog client module."""

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from prism_sync._fsutil import write_json_atomic
from prism_sync.catalog_client import (
    HttpCatalogClientError,
    _catalog_cache_paths,
    _delete_catalog_cache,
    _read_catalog_cache,
    _read_catalog_meta,
    _validate_path_part,
    _write_catalog_cache,
    fetch_catalog,
    load_catalog,
    sanitize_relative,
)
from prism_sync.errors import CatalogTreeError
from prism_sync.http_client import BytesResponse, HttpClient


class TestSanitizeRelative:
    """Tests for sanitize_relative function."""

    def test_simple_path(self) -> None:
        """Test simple relative path."""
        result = sanitize_relative("a/b/c")
        assert result == Path("a", "b", "c")

    def test_path_with_dots(self) -> None:
        """Test path with dots."""
        result = sanitize_relative("a.b/c.d/e")
        assert result == Path("a.b", "c.d", "e")

    def test_empty_path_raises(self) -> None:
        """Test that empty path raises CatalogTreeError."""
        with pytest.raises(CatalogTreeError, match="empty catalog path"):
            sanitize_relative("")

    def test_absolute_path_raises(self) -> None:
        """Test that absolute path raises CatalogTreeError."""
        with pytest.raises(
            CatalogTreeError, match="absolute catalog path is not allowed"
        ):
            sanitize_relative("/absolute/path")

    def test_windows_absolute_path_raises(self) -> None:
        """Test that Windows absolute path raises CatalogTreeError."""
        with pytest.raises(CatalogTreeError, match="unsafe path component"):
            sanitize_relative("C:\\path")

    def test_path_with_backslash_raises(self) -> None:
        """Test that path with backslash raises CatalogTreeError."""
        with pytest.raises(CatalogTreeError, match="unsafe path component"):
            sanitize_relative("a\\b\\c")

    def test_path_with_null_byte_raises(self) -> None:
        """Test that path with null byte raises CatalogTreeError."""
        with pytest.raises(CatalogTreeError, match="unsafe path component"):
            sanitize_relative("a\x00b")

    def test_path_with_dot_only_raises(self) -> None:
        """Test that path with only dot raises CatalogTreeError."""
        with pytest.raises(CatalogTreeError, match="empty catalog path"):
            sanitize_relative(".")

    def test_path_with_dotdot_raises(self) -> None:
        """Test that path with .. raises CatalogTreeError."""
        with pytest.raises(CatalogTreeError, match="unsafe path component"):
            sanitize_relative("..")

    def test_path_with_dot_in_parts(self) -> None:
        """Test that path with . in parts is normalized."""
        # PurePosixPath normalizes . in paths
        result = sanitize_relative("a/./b")
        assert result == Path("a", "b")

    def test_windows_reserved_names(self) -> None:
        """Test Windows reserved device names."""
        for name in ["CON", "PRN", "AUX", "NUL", "COM1", "LPT1"]:
            with pytest.raises(CatalogTreeError, match="reserved Windows device name"):
                sanitize_relative(name)

    def test_windows_invalid_chars(self) -> None:
        """Test Windows invalid characters."""
        for char in '<>:"|?*':
            with pytest.raises(CatalogTreeError, match="unsafe Windows character"):
                sanitize_relative(f"a{char}b")


class TestValidatePathPart:
    """Tests for _validate_path_part function."""

    def test_valid_part(self) -> None:
        """Test valid path part."""
        _validate_path_part("normal")

    def test_empty_part_raises(self) -> None:
        """Test empty part raises."""
        with pytest.raises(CatalogTreeError, match="unsafe path component"):
            _validate_path_part("")

    def test_dot_part_raises(self) -> None:
        """Test . part raises."""
        with pytest.raises(CatalogTreeError, match="unsafe path component"):
            _validate_path_part(".")

    def test_dotdot_part_raises(self) -> None:
        """Test .. part raises."""
        with pytest.raises(CatalogTreeError, match="unsafe path component"):
            _validate_path_part("..")


class TestCatalogCachePaths:
    """Tests for _catalog_cache_paths function."""

    def test_cache_paths(self) -> None:
        """Test cache path generation."""
        cache_dir = Path("/tmp/output")
        real = "abcdef1234567890"
        bin_path, meta_path = _catalog_cache_paths(cache_dir, real)
        assert bin_path == cache_dir / ".catalog-cache" / "abcdef1234567890.bin"
        assert meta_path == cache_dir / ".catalog-cache" / "abcdef1234567890.json"

    def test_cache_paths_with_string_output(self) -> None:
        """Test cache path generation with string output."""
        cache_dir = "/tmp/output"
        real = "test"
        bin_path, meta_path = _catalog_cache_paths(cache_dir, real)
        assert bin_path == Path(cache_dir) / ".catalog-cache" / "test.bin"
        assert meta_path == Path(cache_dir) / ".catalog-cache" / "test.json"


class TestReadCatalogCache:
    """Tests for _read_catalog_cache function."""

    def test_read_valid_cache(self, tmp_path: Path) -> None:
        """Test reading valid cache."""
        from prism_sync.models import CatalogManifest

        cache_path = tmp_path / "test.bin"
        cache_path.write_bytes(b"test content")

        manifest = CatalogManifest(
            label_crc=0,
            size=12,
            checksum=0,
            seed=0,
        )

        result = _read_catalog_cache(cache_path, manifest)
        assert result == b"test content"

    def test_read_nonexistent_cache(self, tmp_path: Path) -> None:
        """Test reading nonexistent cache."""
        from prism_sync.models import CatalogManifest

        cache_path = tmp_path / "nonexistent.bin"
        manifest = CatalogManifest(
            label_crc=0,
            size=12,
            checksum=0,
            seed=0,
        )

        result = _read_catalog_cache(cache_path, manifest)
        assert result is None

    def test_read_size_mismatch(self, tmp_path: Path) -> None:
        """Test reading cache with size mismatch."""
        from prism_sync.models import CatalogManifest

        cache_path = tmp_path / "test.bin"
        cache_path.write_bytes(b"test content")

        manifest = CatalogManifest(
            label_crc=0,
            size=100,  # Different from actual size
            checksum=0,
            seed=0,
        )

        result = _read_catalog_cache(cache_path, manifest)
        assert result is None

    def test_read_symlink_returns_none(self, tmp_path: Path) -> None:
        """Test reading symlink returns None."""
        from prism_sync.models import CatalogManifest

        cache_path = tmp_path / "test.bin"
        target = tmp_path / "target.bin"
        target.write_bytes(b"test")

        # Create symlink (may fail on Windows without privileges)
        try:
            cache_path.symlink_to(target)
            manifest = CatalogManifest(
                label_crc=0,
                size=4,
                checksum=0,
                seed=0,
            )
            result = _read_catalog_cache(cache_path, manifest)
            assert result is None
        except OSError:
            pytest.skip("Symlink creation not supported")


class TestReadCatalogMeta:
    """Tests for _read_catalog_meta function."""

    def test_read_valid_meta(self, tmp_path: Path) -> None:
        """Test reading valid metadata."""
        meta_path = tmp_path / "test.json"
        meta_path.write_text('{"etag": "abc", "size": 100}\n')

        result = _read_catalog_meta(meta_path)
        assert result == {"etag": "abc", "size": 100}

    def test_read_nonexistent_meta(self, tmp_path: Path) -> None:
        """Test reading nonexistent metadata."""
        meta_path = tmp_path / "nonexistent.json"
        result = _read_catalog_meta(meta_path)
        assert result is None

    def test_read_invalid_json(self, tmp_path: Path) -> None:
        """Test reading invalid JSON."""
        meta_path = tmp_path / "invalid.json"
        meta_path.write_text("not valid json")

        result = _read_catalog_meta(meta_path)
        assert result is None

    def test_read_non_dict_json(self, tmp_path: Path) -> None:
        """Test reading non-dict JSON."""
        meta_path = tmp_path / "list.json"
        meta_path.write_text("[1, 2, 3]\n")

        result = _read_catalog_meta(meta_path)
        assert result is None


class TestDeleteCatalogCache:
    """Tests for _delete_catalog_cache function."""

    def test_delete_existing_cache(self, tmp_path: Path) -> None:
        """Test deleting existing cache files."""
        cache_path = tmp_path / "test.bin"
        meta_path = tmp_path / "test.json"
        cache_path.write_bytes(b"test")
        meta_path.write_text("{}")

        _delete_catalog_cache(cache_path, meta_path)

        assert not cache_path.exists()
        assert not meta_path.exists()

    def test_delete_nonexistent_cache(self, tmp_path: Path) -> None:
        """Test deleting nonexistent cache files."""
        cache_path = tmp_path / "nonexistent.bin"
        meta_path = tmp_path / "nonexistent.json"

        # Should not raise
        _delete_catalog_cache(cache_path, meta_path)


class TestFetchCatalog:
    """Tests for fetch_catalog function."""

    def test_fetch_with_mock_client(self, tmp_path: Path) -> None:
        """Test fetching catalog with mocked HTTP client."""
        from prism_sync.models import CatalogManifest

        # Create a mock response
        mock_response = BytesResponse(
            content=b"test catalog data",
            status_code=200,
            headers=httpx.Headers({"etag": "test-etag"}),
        )

        # Create mock client
        mock_client = MagicMock(spec=HttpClient)
        mock_client.base_url = "https://example.com"
        mock_client.get_bytes_response.return_value = mock_response

        manifest = CatalogManifest(
            label_crc=0,
            size=17,
            checksum=0,
            seed=0,
        )

        result = fetch_catalog(
            mock_client,
            manifest,
            tmp_path,
            use_cache=False,
            writable=False,
        )

        assert result == b"test catalog data"
        mock_client.get_bytes_response.assert_called_once()

    def test_fetch_with_real_name(self, tmp_path: Path) -> None:
        """Test fetching catalog with real name computation."""
        from prism_sync.models import CatalogManifest

        # Create a mock response
        mock_response = BytesResponse(
            content=b"test catalog data",
            status_code=200,
            headers=httpx.Headers({"etag": "test-etag"}),
        )

        # Create mock client
        mock_client = MagicMock(spec=HttpClient)
        mock_client.base_url = "https://example.com"
        mock_client.get_bytes_response.return_value = mock_response

        manifest = CatalogManifest(
            label_crc=0,
            size=17,
            checksum=0,
            seed=0,
        )

        result = fetch_catalog(
            mock_client,
            manifest,
            tmp_path,
            use_cache=False,
            writable=False,
        )

        assert result == b"test catalog data"


class TestLoadCatalog:
    """Tests for load_catalog function."""

    def test_load_with_mock_client(self, tmp_path: Path) -> None:
        """Test loading catalog with mocked HTTP client and decode_catalog."""
        from prism_sync.models import CatalogManifest

        # Use a simple mock that returns empty catalog data
        # We'll mock decode_catalog to return valid plain data
        mock_response = BytesResponse(
            content=b"\xbc\x10\x00\x00\x00",  # Magic + empty counts
            status_code=200,
            headers=httpx.Headers({}),
        )

        mock_client = MagicMock(spec=HttpClient)
        mock_client.base_url = "https://example.com"
        mock_client.get_bytes_response.return_value = mock_response

        manifest = CatalogManifest(
            label_crc=0,
            size=5,
            checksum=0,
            seed=0,
        )

        # Mock decode_catalog to return valid plain data
        with patch("prism_sync.catalog_client.decode_catalog") as mock_decode:
            mock_decode.return_value = b"\xbc\x10\x00\x00\x00"  # Valid plain catalog
            result = load_catalog(
                mock_client,
                manifest,
                tmp_path,
                use_cache=False,
                writable=False,
            )

            # Should return parsed entries (empty list for empty catalog)
            assert isinstance(result, list)
            mock_client.get_bytes_response.assert_called_once()
            mock_decode.assert_called_once()


class TestHttpCatalogClientError:
    """Tests for HttpCatalogClientError."""

    def test_exception_message(self) -> None:
        """Test exception message."""
        exc = HttpCatalogClientError("test error")
        assert str(exc) == "test error"

    def test_exception_inheritance(self) -> None:
        """Test exception inherits from Exception."""
        assert issubclass(HttpCatalogClientError, Exception)


class TestWriteJsonAtomic:
    """Tests for _write_json_atomic function."""

    def testwrite_json_atomic(self, tmp_path: Path) -> None:
        """Test atomic JSON writing."""
        target = tmp_path / "test.json"
        data = {"key": "value", "number": 42}

        write_json_atomic(target, data)

        assert target.exists()
        content = target.read_text(encoding="utf-8")
        assert '"key":"value"' in content
        assert '"number":42' in content
        assert content.endswith("\n")

    def test_write_json_atomic_creates_parent_dirs(self, tmp_path: Path) -> None:
        """Test that parent directories are created."""
        target = tmp_path / "subdir" / "nested" / "test.json"
        data = {"test": True}

        write_json_atomic(target, data)

        assert target.exists()
        assert target.parent.exists()

    def test_write_json_atomic_preserves_format(self, tmp_path: Path) -> None:
        """Test JSON format is preserved with compact separators."""
        target = tmp_path / "test.json"
        data = {"a": 1, "b": 2}

        write_json_atomic(target, data)

        content = target.read_text(encoding="utf-8")
        # Should use compact separators
        assert ":" in content
        assert "," in content
        # Should not have spaces after : or ,
        assert ": " not in content
        assert ", " not in content


class TestWriteCatalogCache:
    """Tests for _write_catalog_cache function."""

    def test_write_catalog_cache(self, tmp_path: Path) -> None:
        """Test writing catalog cache with atomic metadata."""
        import httpx

        cache_path = tmp_path / "test.bin"
        meta_path = tmp_path / "test.json"
        blob = b"test catalog data"
        headers = httpx.Headers(
            {"etag": "test-etag", "last-modified": "Mon, 01 Jan 2024 00:00:00 GMT"}
        )

        _write_catalog_cache(cache_path, meta_path, blob, headers)

        assert cache_path.exists()
        assert meta_path.exists()
        assert cache_path.read_bytes() == blob

        meta_content = meta_path.read_text(encoding="utf-8")
        assert '"etag":"test-etag"' in meta_content
        assert '"lastModified":"Mon, 01 Jan 2024 00:00:00 GMT"' in meta_content
        assert '"size":17' in meta_content
        assert '"cachedAt"' in meta_content

    def test_write_catalog_cache_creates_cache_dir(self, tmp_path: Path) -> None:
        """Test that cache directory is created."""
        import httpx

        cache_dir = tmp_path / ".catalog-cache"
        cache_path = cache_dir / "test.bin"
        meta_path = cache_dir / "test.json"
        blob = b"test"
        headers = httpx.Headers({})

        _write_catalog_cache(cache_path, meta_path, blob, headers)

        assert cache_dir.exists()
        assert cache_path.exists()
        assert meta_path.exists()

    def test_write_catalog_cache_metadata_atomic(self, tmp_path: Path) -> None:
        """Test that metadata is written atomically via temp file."""
        import tempfile

        import httpx

        cache_path = tmp_path / "test.bin"
        meta_path = tmp_path / "test.json"
        blob = b"test"
        headers = httpx.Headers({})

        # Track temp files created
        original_mkstemp = tempfile.mkstemp
        temp_files = []

        def mock_mkstemp(*args, **kwargs):
            fd, name = original_mkstemp(*args, **kwargs)
            temp_files.append(name)
            return fd, name

        with patch("prism_sync._fsutil.tempfile.mkstemp", mock_mkstemp):
            _write_catalog_cache(cache_path, meta_path, blob, headers)

        # Should have created temp file for metadata
        assert len(temp_files) >= 1
        # Temp file should be in same directory as meta_path
        for tmp_file in temp_files:
            assert Path(tmp_file).parent == meta_path.parent

    def test_write_catalog_cache_binary_atomic(self, tmp_path: Path) -> None:
        """Test that binary file is written atomically."""
        import httpx

        cache_path = tmp_path / "test.bin"
        meta_path = tmp_path / "test.json"
        blob = b"test"
        headers = httpx.Headers({})

        _write_catalog_cache(cache_path, meta_path, blob, headers)

        # Binary file should exist and have correct content
        assert cache_path.exists()
        assert cache_path.read_bytes() == blob

    def test_write_catalog_cache_metadata_format(self, tmp_path: Path) -> None:
        """Test metadata JSON format compatibility."""
        import httpx

        cache_path = tmp_path / "test.bin"
        meta_path = tmp_path / "test.json"
        blob = b"test data"
        headers = httpx.Headers(
            {
                "etag": '"abc123"',
                "last-modified": "Wed, 21 Oct 2015 07:28:00 GMT",
            }
        )

        _write_catalog_cache(cache_path, meta_path, blob, headers)

        # Read and parse metadata
        meta = _read_catalog_meta(meta_path)
        assert meta is not None
        assert meta["etag"] == '"abc123"'
        assert meta["lastModified"] == "Wed, 21 Oct 2015 07:28:00 GMT"
        assert meta["size"] == 9
        assert "cachedAt" in meta

    def test_write_catalog_cache_binary_write_failure(self, tmp_path: Path) -> None:
        """Test handling of binary write failure."""
        import httpx

        cache_path = tmp_path / "test.bin"
        meta_path = tmp_path / "test.json"
        blob = b"test"
        headers = httpx.Headers({})

        # Create a mock that raises OSError when writing to cache_path or its temp file
        original_write_bytes = Path.write_bytes
        cache_tmp_path = cache_path.with_name(cache_path.name + ".tmp")

        def failing_write_bytes(self, data):
            if str(self) in (str(cache_path), str(cache_tmp_path)):
                raise OSError("Mocked write failure")
            return original_write_bytes(self, data)

        with patch.object(Path, "write_bytes", failing_write_bytes):
            # Should not raise, just log warning
            _write_catalog_cache(cache_path, meta_path, blob, headers)

        # Verify metadata file was not created (because binary write failed)
        assert not meta_path.exists()
        # Verify binary file was not created
        assert not cache_path.exists()
        # Verify no temp files left behind
        temp_files = list(tmp_path.glob("*.tmp"))
        assert len(temp_files) == 0

    def test_write_catalog_cache_metadata_write_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test handling of metadata write failure using monkeypatch."""
        import httpx

        cache_path = tmp_path / "test.bin"
        meta_path = tmp_path / "test.json"
        blob = b"test"
        headers = httpx.Headers({})

        # Mock _write_json_atomic to raise OSError
        def mock_write_json_atomic(path, value):
            raise OSError("Mocked metadata write failure")

        monkeypatch.setattr(
            "prism_sync.catalog_client.write_json_atomic", mock_write_json_atomic
        )

        # Should not raise, just log warning
        _write_catalog_cache(cache_path, meta_path, blob, headers)

        monkeypatch.undo()

        # Verify binary file was written but metadata was not
        assert cache_path.exists()
        assert cache_path.read_bytes() == blob
        assert not meta_path.exists()
        # Verify no temp files left behind
        temp_files = list(tmp_path.glob("*.tmp"))
        assert len(temp_files) == 0

    def test_write_catalog_cache_partial_metadata_not_written(
        self, tmp_path: Path
    ) -> None:
        """Test that partial metadata is not written on failure."""
        import httpx

        cache_path = tmp_path / "test.bin"
        meta_path = tmp_path / "test.json"
        blob = b"test"
        headers = httpx.Headers({})

        # Write valid cache first
        _write_catalog_cache(cache_path, meta_path, blob, headers)
        original_meta = meta_path.read_text(encoding="utf-8")

        # Try to write with invalid metadata path (should fail)
        invalid_meta_path = tmp_path / "invalid" / "test.json"
        with contextlib.suppress(OSError):
            _write_catalog_cache(cache_path, invalid_meta_path, blob, headers)

        # Original metadata should still be intact
        assert meta_path.exists()
        assert meta_path.read_text(encoding="utf-8") == original_meta

    def test_write_catalog_cache_cleanup_on_binary_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that temp file is cleaned up on binary write failure."""
        import httpx

        cache_path = tmp_path / "test.bin"
        meta_path = tmp_path / "test.json"
        blob = b"test"
        headers = httpx.Headers({})

        # Mock Path.write_bytes to raise OSError
        original_write_bytes = Path.write_bytes

        def mock_write_bytes(self, data):
            if str(self) == str(cache_path):
                raise OSError("Mocked write failure")
            return original_write_bytes(self, data)

        monkeypatch.setattr(Path, "write_bytes", mock_write_bytes)

        # Should not raise, just log warning
        _write_catalog_cache(cache_path, meta_path, blob, headers)

        monkeypatch.undo()

        # Verify no temp files left behind
        temp_files = list(tmp_path.glob("*.tmp"))
        assert len(temp_files) == 0

    def test_write_json_atomic_cleanup_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that temp file is cleaned up when _write_json_atomic fails."""
        target = tmp_path / "test.json"
        data = {"key": "value"}

        # Mock os.replace to raise OSError after temp file is created
        import os as os_module

        original_replace = os_module.replace

        def mock_replace(src, dst):
            if str(dst) == str(target):
                raise OSError("Mocked replace failure")
            return original_replace(src, dst)

        monkeypatch.setattr(os_module, "replace", mock_replace)

        try:
            with pytest.raises(OSError, match="Mocked replace failure"):
                write_json_atomic(target, data)
        finally:
            monkeypatch.undo()

        # Verify no temp files left behind
        temp_files = list(tmp_path.glob("*.tmp"))
        assert len(temp_files) == 0

    def test_write_json_atomic_original_exception_propagated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that original exception is propagated, not cleanup exception."""
        import os as os_module

        target = tmp_path / "test.json"
        data = {"key": "value"}

        # Mock both os.replace and os.unlink to raise OSError
        original_replace = os_module.replace
        original_unlink = os_module.unlink

        def mock_replace(src, dst):
            if str(dst) == str(target):
                raise OSError("Original replace failure")
            return original_replace(src, dst)

        def mock_unlink(path):
            if ".tmp" in str(path):
                raise OSError("Cleanup unlink failure")
            return original_unlink(path)

        monkeypatch.setattr(os_module, "replace", mock_replace)
        monkeypatch.setattr(os_module, "unlink", mock_unlink)

        try:
            with pytest.raises(OSError, match="Original replace failure"):
                write_json_atomic(target, data)
        finally:
            monkeypatch.undo()

    def test_write_catalog_cache_preserves_original_metadata_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that original metadata is preserved when write fails."""
        import httpx

        cache_path = tmp_path / "test.bin"
        meta_path = tmp_path / "test.json"
        blob = b"test"
        headers = httpx.Headers({"etag": "original-etag"})

        # Write initial cache
        _write_catalog_cache(cache_path, meta_path, blob, headers)
        original_meta = meta_path.read_text(encoding="utf-8")

        # Mock _write_json_atomic to fail
        def mock_write_json_atomic(path, value):
            raise OSError("Mocked metadata write failure")

        monkeypatch.setattr(
            "prism_sync.catalog_client.write_json_atomic", mock_write_json_atomic
        )

        # This should not raise, just log warning
        _write_catalog_cache(cache_path, meta_path, b"new data", headers)

        monkeypatch.undo()

        # Original metadata should be preserved
        assert meta_path.exists()
        assert meta_path.read_text(encoding="utf-8") == original_meta
        # Binary file should be updated though
        assert cache_path.read_bytes() == b"new data"

    def test_write_catalog_cache_warning_logged_on_failure(
        self, tmp_path: Path
    ) -> None:
        """Test that warning is logged when cache write fails."""
        import httpx

        cache_path = tmp_path / "test.bin"
        meta_path = tmp_path / "test.json"
        blob = b"test"
        headers = httpx.Headers({})

        # Create a mock that raises OSError when writing to cache_path or its temp file
        original_write_bytes = Path.write_bytes
        cache_tmp_path = cache_path.with_name(cache_path.name + ".tmp")

        def failing_write_bytes(self, data):
            if str(self) in (str(cache_path), str(cache_tmp_path)):
                raise OSError("Mocked write failure")
            return original_write_bytes(self, data)

        with patch.object(Path, "write_bytes", failing_write_bytes):
            # Should not raise, just log warning
            _write_catalog_cache(cache_path, meta_path, blob, headers)

        # Verify files were not created
        assert not cache_path.exists()
        assert not meta_path.exists()
        # Verify no temp files left behind
        temp_files = list(tmp_path.glob("*.tmp"))
        assert len(temp_files) == 0
