"""Tests for the sync state snapshot store."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from prism_sync import SyncStateStore


def make_path(tmp_path: Path) -> Path:
    """Return a snapshot path inside pytest's temporary directory."""
    return tmp_path / "sync-state.json"


class TestLoad:
    """Tests for ``SyncStateStore.load``."""

    def test_missing_file_returns_empty_dict(self, tmp_path: Path) -> None:
        """A missing snapshot yields an empty mapping."""
        store = SyncStateStore(make_path(tmp_path))

        assert store.load() == {}

    def test_reads_json_object(self, tmp_path: Path) -> None:
        """A JSON object is returned as a plain dict."""
        path = make_path(tmp_path)
        path.write_text('{"status": "OK", "files": {}}', encoding="utf-8")

        assert SyncStateStore(path).load() == {"status": "OK", "files": {}}

    @pytest.mark.parametrize(
        "payload", ["[]", "[1, 2]", '"text"', "42", "null", "true"]
    )
    def test_non_dict_root_raises_value_error(
        self, tmp_path: Path, payload: str
    ) -> None:
        """A non-object JSON root is rejected, not silently accepted."""
        path = make_path(tmp_path)
        path.write_text(payload, encoding="utf-8")

        with pytest.raises(ValueError, match="state file is not a JSON object"):
            SyncStateStore(path).load()

    def test_corrupt_json_propagates_decode_error(self, tmp_path: Path) -> None:
        """Malformed JSON surfaces as ``JSONDecodeError`` rather than ``{}``."""
        path = make_path(tmp_path)
        path.write_text('{"status": "OK",', encoding="utf-8")

        with pytest.raises(json.JSONDecodeError):
            SyncStateStore(path).load()

    def test_empty_file_propagates_decode_error(self, tmp_path: Path) -> None:
        """An empty file is invalid JSON and is not treated as ``{}``."""
        path = make_path(tmp_path)
        path.write_text("", encoding="utf-8")

        with pytest.raises(json.JSONDecodeError):
            SyncStateStore(path).load()

    def test_accepts_string_path(self, tmp_path: Path) -> None:
        """``str`` paths are accepted and normalized internally."""
        path = make_path(tmp_path)
        path.write_text('{"a": 1}', encoding="utf-8")

        store = SyncStateStore(str(path))

        assert isinstance(store.path, Path)
        assert store.load() == {"a": 1}


class TestSave:
    """Tests for ``SyncStateStore.save``."""

    def test_roundtrip(self, tmp_path: Path) -> None:
        """Saved state reads back identically."""
        store = SyncStateStore(make_path(tmp_path))
        state = {"status": "OK", "files": {"root/a": {"size": 3}}}

        store.save(state)

        assert store.load() == state

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        """Missing parent directories are created."""
        path = tmp_path / "nested" / "deeper" / "sync-state.json"
        store = SyncStateStore(path)

        store.save({"a": 1})

        assert path.is_file()
        assert store.load() == {"a": 1}

    def test_writes_compact_json(self, tmp_path: Path) -> None:
        """Output uses compact separators and no indentation."""
        path = make_path(tmp_path)
        SyncStateStore(path).save({"a": 1, "b": [1, 2]})

        text = path.read_text(encoding="utf-8")

        assert ": " not in text
        assert ", " not in text
        assert "\n  " not in text
        assert text.startswith('{"a":1')

    def test_writes_trailing_newline(self, tmp_path: Path) -> None:
        """The file ends with exactly one newline."""
        path = make_path(tmp_path)
        SyncStateStore(path).save({"a": 1})

        text = path.read_text(encoding="utf-8")

        assert text.endswith("\n")
        assert not text.endswith("\n\n")

    def test_does_not_escape_non_ascii(self, tmp_path: Path) -> None:
        """``ensure_ascii=False`` keeps non-ASCII text readable."""
        path = make_path(tmp_path)
        SyncStateStore(path).save({"note": "资源"})

        assert "资源" in path.read_text(encoding="utf-8")

    def test_preserves_field_names_and_values(self, tmp_path: Path) -> None:
        """Saving does not rename, drop or retype any field."""
        path = make_path(tmp_path)
        state: dict[str, Any] = {
            "checkedAt": "2026-01-01T00:00:00+00:00",
            "status": "PARTIAL",
            "gameVersion": "1.2.3",
            "resourceVersion": "abc@def",
            "catalogFingerprint": "f" * 64,
            "summary": {"downloaded": 2, "failed": 1},
            "selection": {"active": True, "types": ["asset"]},
            "files": {
                "root/a.bin": {
                    "size": 10,
                    "rawSize": 10,
                    "realName": "abcdef",
                    "status": "downloaded",
                    "verified": True,
                    "rawMd5": "0" * 32,
                    "sha256": "1" * 64,
                    "etag": "abc",
                }
            },
        }

        SyncStateStore(path).save(state)

        assert SyncStateStore(path).load() == state
        assert json.loads(path.read_text(encoding="utf-8")) == state

    def test_overwrites_previous_snapshot(self, tmp_path: Path) -> None:
        """A second save replaces the first atomically."""
        path = make_path(tmp_path)
        store = SyncStateStore(path)
        store.save({"generation": 1})

        store.save({"generation": 2})

        assert store.load() == {"generation": 2}

    def test_write_failure_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing write raises instead of pretending to succeed."""
        path = make_path(tmp_path)
        store = SyncStateStore(path)

        def failing_write(target: Path, value: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("prism_sync.state.write_json_atomic", failing_write)

        with pytest.raises(OSError, match="disk full"):
            store.save({"a": 1})

        assert not path.exists()

    def test_failed_save_keeps_previous_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed save leaves the previous file untouched."""
        path = make_path(tmp_path)
        store = SyncStateStore(path)
        store.save({"generation": 1})
        previous = path.read_text(encoding="utf-8")

        def failing_write(target: Path, value: Any) -> None:
            raise OSError("boom")

        monkeypatch.setattr("prism_sync.state.write_json_atomic", failing_write)

        with pytest.raises(OSError, match="boom"):
            store.save({"generation": 2})

        monkeypatch.undo()
        assert path.read_text(encoding="utf-8") == previous


class TestOriginalProjectFormat:
    """The store must read snapshots produced by the original project."""

    def test_reads_original_snapshot(self, tmp_path: Path) -> None:
        """A snapshot with the original field structure loads unchanged."""
        path = make_path(tmp_path)
        original = {
            "checkedAt": "2026-11-18T00:00:00.123456+00:00",
            "status": "OK",
            "gameVersion": "2.19.1",
            "resourceVersion": "simple@AAAA",
            "catalogFingerprint": "0" * 64,
            "summary": {
                "catalogs": 3,
                "assets": 120,
                "totalAssets": 130,
                "downloaded": 100,
                "skipped": 20,
                "failed": 0,
                "resumed": 4,
                "pruned": 2,
                "localizetextCategories": 5,
            },
            "selection": None,
            "files": {
                "root/raw/a.bytes": {
                    "size": 12,
                    "rawSize": 12,
                    "realName": "aa00bb11",
                    "status": "downloaded",
                    "verified": True,
                    "rawMd5": "d41d8cd98f00b204e9800998ecf8427e",
                    "sha256": "e3b0c44298fc1c149afbf4c8996fb924",
                    "etag": None,
                },
                "root/raw/b.bytes": {
                    "size": None,
                    "realName": "cc22dd33",
                    "status": "failed",
                    "error": "HTTP 404",
                },
            },
        }
        path.write_text(
            json.dumps(original, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        loaded = SyncStateStore(path).load()

        assert loaded == original
        assert loaded["files"]["root/raw/b.bytes"]["status"] == "failed"
        assert loaded["files"]["root/raw/b.bytes"]["size"] is None

    def test_roundtrip_preserves_none_and_bool(self, tmp_path: Path) -> None:
        """``None`` and booleans survive a save/load cycle unchanged."""
        path = make_path(tmp_path)
        store = SyncStateStore(path)
        state = {
            "selection": None,
            "files": {"a": {"verified": False, "etag": None, "size": None}},
        }

        store.save(state)

        loaded = store.load()
        assert loaded["selection"] is None
        assert loaded["files"]["a"]["verified"] is False
        assert loaded["files"]["a"]["etag"] is None
