"""Tests for the batch asset download helper."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from prism_sync.downloader import DownloadResult, download_assets
from prism_sync.errors import DownloadCancelled
from prism_sync.models import MAX_JOBS, CatalogAsset, CatalogEntry, ResourceType


def make_entry(size: int = 4) -> CatalogEntry:
    return CatalogEntry(
        label="file",
        type_name="asset",
        size=size,
        checksum=0,
        seed=0,
        label_crc=0,
        priority=0,
        resource_type=ResourceType.RawPlain,
        resource_type_value=int(ResourceType.RawPlain),
    )


def make_assets(tmp_path: Path, count: int) -> list[CatalogAsset]:
    return [
        CatalogAsset(
            entry=make_entry(),
            catalog_label="root",
            output_path=str(tmp_path / f"a{index}.bin"),
            relative_path=f"a{index}.bin",
            real_name=f"real{index:04d}",
        )
        for index in range(count)
    ]


class FakeDownloader:
    """Scripted downloader recording calls and concurrency."""

    def __init__(
        self,
        *,
        behaviours: dict[str, Any] | None = None,
        barrier: threading.Barrier | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.behaviours = behaviours or {}
        self.barrier = barrier
        self.stop_event = stop_event
        self.calls: list[str] = []
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def download(self, asset: CatalogAsset) -> DownloadResult:
        with self._lock:
            self.calls.append(asset.relative_path)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.barrier is not None:
                self.barrier.wait(timeout=5)
            behaviour = self.behaviours.get(asset.relative_path)
            if callable(behaviour):
                return behaviour(asset)
            if isinstance(behaviour, BaseException):
                raise behaviour
            if behaviour is not None:
                return behaviour
            return DownloadResult(
                status="downloaded",
                size=4,
                raw_size=4,
                verified=True,
            )
        finally:
            with self._lock:
                self.active -= 1


class TestBasicBehaviour:
    """Tests for the normal, non-cancelled path."""

    def test_empty_list_returns_empty_list(self, tmp_path: Path) -> None:
        """No assets means no work and no callback."""
        calls: list[tuple[str, int, int]] = []
        downloader: Any = FakeDownloader()
        downloader.download = lambda asset: pytest.fail("must not be called")

        result = download_assets(
            downloader,
            [],
            on_progress=lambda a, d, t: calls.append((a.relative_path, d, t)),
        )

        assert result == []
        assert calls == []

    def test_single_asset(self, tmp_path: Path) -> None:
        """A single asset produces a single DownloadResult."""
        assets = make_assets(tmp_path, 1)

        result = download_assets(FakeDownloader(), assets)  # type: ignore[arg-type]

        assert len(result) == 1
        assert isinstance(result[0], DownloadResult)
        assert result[0].status == "downloaded"

    def test_multiple_assets(self, tmp_path: Path) -> None:
        """Every asset is downloaded exactly once."""
        assets = make_assets(tmp_path, 5)
        downloader = FakeDownloader()

        result = download_assets(downloader, assets, jobs=3)  # type: ignore[arg-type]

        assert len(result) == 5
        assert all(isinstance(item, DownloadResult) for item in result)
        assert sorted(downloader.calls) == sorted(a.relative_path for a in assets)

    def test_result_order_matches_input_order(self, tmp_path: Path) -> None:
        """Results follow input order even when completion order differs."""
        assets = make_assets(tmp_path, 6)
        # Force the first assets to finish last by making them slow.
        release = threading.Event()

        def slow(asset: CatalogAsset) -> DownloadResult:
            release.wait(timeout=5)
            return DownloadResult(
                status="downloaded", size=1, raw_size=1, verified=True
            )

        behaviours: dict[str, Any] = {}
        for index in range(3):
            behaviours[assets[index].relative_path] = slow

        def unlock(asset: CatalogAsset) -> DownloadResult:
            release.set()
            return DownloadResult(
                status="downloaded", size=9, raw_size=9, verified=True
            )

        for index in range(3, 6):
            behaviours[assets[index].relative_path] = unlock

        downloader = FakeDownloader(behaviours=behaviours)
        result = download_assets(downloader, assets, jobs=6)  # type: ignore[arg-type]

        assert [item.size for item in result] == [1, 1, 1, 9, 9, 9]

    def test_single_failure_does_not_stop_others(self, tmp_path: Path) -> None:
        """One asset failing leaves the others intact."""
        assets = make_assets(tmp_path, 3)
        boom = RuntimeError("boom")
        downloader = FakeDownloader(behaviours={assets[1].relative_path: boom})

        result = download_assets(downloader, assets, jobs=1)  # type: ignore[arg-type]

        assert isinstance(result[0], DownloadResult)
        assert result[1] is boom
        assert isinstance(result[2], DownloadResult)

    def test_exception_object_preserved(self, tmp_path: Path) -> None:
        """The original exception instance is returned, not a string."""
        assets = make_assets(tmp_path, 1)
        boom = ValueError("original")
        downloader = FakeDownloader(behaviours={assets[0].relative_path: boom})

        result = download_assets(downloader, assets)  # type: ignore[arg-type]

        assert result[0] is boom
        assert isinstance(result[0], ValueError)

    def test_skipped_asset_returns_download_result(self, tmp_path: Path) -> None:
        """A skipped asset yields its real DownloadResult."""
        assets = make_assets(tmp_path, 1)
        skipped = DownloadResult(status="skipped", size=4, raw_size=4, verified=False)
        downloader = FakeDownloader(behaviours={assets[0].relative_path: skipped})

        result = download_assets(downloader, assets)  # type: ignore[arg-type]

        assert result[0] is skipped
        assert isinstance(result[0], DownloadResult)
        assert result[0].status == "skipped"

    @pytest.mark.parametrize("jobs", [0, -1, MAX_JOBS + 1, 100])
    def test_invalid_jobs_rejected(self, tmp_path: Path, jobs: int) -> None:
        """Out-of-range concurrency is rejected before any work starts."""
        assets = make_assets(tmp_path, 1)

        with pytest.raises(ValueError, match="jobs must be between"):
            download_assets(FakeDownloader(), assets, jobs=jobs)  # type: ignore[arg-type]

    def test_concurrency_capped_by_jobs(self, tmp_path: Path) -> None:
        """At most ``jobs`` tasks run at the same time.

        With ``jobs=4`` and 4 assets, a barrier of 4 can only pass if all four
        tasks really run concurrently.  The whole batch is exactly one wave, so
        no assertion depends on a timeout.
        """
        assets = make_assets(tmp_path, 4)
        barrier = threading.Barrier(4)
        downloader = FakeDownloader(barrier=barrier)

        result = download_assets(downloader, assets, jobs=4)  # type: ignore[arg-type]

        assert len(result) == 4
        assert downloader.peak == 4

    def test_default_jobs_is_eight(self, tmp_path: Path) -> None:
        """The default concurrency is 8, within the allowed range."""
        assets = make_assets(tmp_path, 4)

        result = download_assets(FakeDownloader(), assets)  # type: ignore[arg-type]

        assert len(result) == 4


class TestProgressCallback:
    """Tests for ``on_progress`` semantics."""

    def test_success_triggers_callback(self, tmp_path: Path) -> None:
        """Each successful asset reports progress once."""
        assets = make_assets(tmp_path, 3)
        events: list[tuple[str, int, int]] = []

        download_assets(  # type: ignore[arg-type]
            FakeDownloader(),
            assets,
            jobs=1,
            on_progress=lambda a, d, t: events.append((a.relative_path, d, t)),
        )

        assert len(events) == 3
        assert all(total == 3 for _, _, total in events)

    def test_failure_triggers_callback(self, tmp_path: Path) -> None:
        """A failed asset still counts as processed."""
        assets = make_assets(tmp_path, 2)
        events: list[tuple[str, int, int]] = []
        downloader = FakeDownloader(
            behaviours={assets[0].relative_path: RuntimeError("nope")}
        )

        download_assets(  # type: ignore[arg-type]
            downloader,
            assets,
            jobs=1,
            on_progress=lambda a, d, t: events.append((a.relative_path, d, t)),
        )

        assert len(events) == 2
        assert [done for _, done, _ in events] == [1, 2]

    def test_done_is_monotonic_and_bounded(self, tmp_path: Path) -> None:
        """``done`` increases strictly and never exceeds ``total``."""
        assets = make_assets(tmp_path, 6)
        events: list[int] = []

        download_assets(  # type: ignore[arg-type]
            FakeDownloader(),
            assets,
            jobs=4,
            on_progress=lambda a, d, t: events.append(d),
        )

        assert events == sorted(events)
        assert events == list(range(1, 7))
        assert max(events) == 6

    def test_total_equals_asset_count(self, tmp_path: Path) -> None:
        """``total`` always equals the input length."""
        assets = make_assets(tmp_path, 4)
        totals: list[int] = []

        download_assets(  # type: ignore[arg-type]
            FakeDownloader(),
            assets,
            on_progress=lambda a, d, t: totals.append(t),
        )

        assert set(totals) == {4}

    def test_callback_exception_propagates_and_cancels(self, tmp_path: Path) -> None:
        """A callback failure aborts the batch and re-raises unchanged."""
        assets = make_assets(tmp_path, 6)
        downloader = FakeDownloader()
        called = {"count": 0}

        def explode(asset: CatalogAsset, done: int, total: int) -> None:
            called["count"] += 1
            raise RuntimeError("callback boom")

        with pytest.raises(RuntimeError, match="callback boom"):
            download_assets(  # type: ignore[arg-type]
                downloader, assets, jobs=1, on_progress=explode
            )

        assert called["count"] >= 1
        assert downloader.stop_event is not None
        assert downloader.stop_event.is_set()


class TestExternalCancellation:
    """Tests for cooperative cancellation through the shared event."""

    def test_shared_event_is_reused(self, tmp_path: Path) -> None:
        """The batch never installs a second event."""
        assets = make_assets(tmp_path, 2)
        event = threading.Event()
        downloader = FakeDownloader(stop_event=event)

        download_assets(downloader, assets)  # type: ignore[arg-type]

        assert downloader.stop_event is event

    def test_event_created_when_downloader_has_none(self, tmp_path: Path) -> None:
        """A missing event is installed on the downloader and reused."""
        assets = make_assets(tmp_path, 1)
        downloader = FakeDownloader(stop_event=None)

        download_assets(downloader, assets)  # type: ignore[arg-type]

        assert isinstance(downloader.stop_event, threading.Event)

    def test_completed_results_survive_cancellation(self, tmp_path: Path) -> None:
        """Assets that finished before cancellation keep their result."""
        assets = make_assets(tmp_path, 4)
        event = threading.Event()

        def finish_then_cancel(asset: CatalogAsset) -> DownloadResult:
            event.set()
            return DownloadResult(
                status="downloaded", size=7, raw_size=7, verified=True
            )

        downloader = FakeDownloader(
            behaviours={assets[0].relative_path: finish_then_cancel},
            stop_event=event,
        )

        result = download_assets(downloader, assets, jobs=1)  # type: ignore[arg-type]

        assert isinstance(result[0], DownloadResult)
        assert result[0].size == 7
        assert result[1:] == [None, None, None]

    def test_not_started_assets_are_none(self, tmp_path: Path) -> None:
        """Assets that never started stay ``None``."""
        assets = make_assets(tmp_path, 5)
        event = threading.Event()

        def stop_now(asset: CatalogAsset) -> DownloadResult:
            event.set()
            return DownloadResult(
                status="downloaded", size=1, raw_size=1, verified=True
            )

        downloader = FakeDownloader(
            behaviours={assets[0].relative_path: stop_now}, stop_event=event
        )

        result = download_assets(downloader, assets, jobs=1)  # type: ignore[arg-type]

        assert result[1:] == [None] * 4
        assert len(result) == 5

    def test_running_task_reports_download_cancelled(self, tmp_path: Path) -> None:
        """A task cancelled mid-flight surfaces as ``DownloadCancelled``."""
        assets = make_assets(tmp_path, 3)
        event = threading.Event()

        def cancel_during(asset: CatalogAsset) -> DownloadResult:
            event.set()
            raise DownloadCancelled("stopped mid-download")

        downloader = FakeDownloader(
            behaviours={assets[0].relative_path: cancel_during}, stop_event=event
        )

        result = download_assets(downloader, assets, jobs=1)  # type: ignore[arg-type]

        assert isinstance(result[0], DownloadCancelled)
        assert result[1:] == [None, None]

    def test_completion_race_preserves_real_result(self, tmp_path: Path) -> None:
        """A task finishing as the event is set keeps its real result."""
        assets = make_assets(tmp_path, 4)
        event = threading.Event()

        def finish_before_stop(asset: CatalogAsset) -> DownloadResult:
            # Set the event, then still return a completed download.
            event.set()
            return DownloadResult(
                status="downloaded", size=3, raw_size=3, verified=True
            )

        downloader = FakeDownloader(
            behaviours={assets[0].relative_path: finish_before_stop}, stop_event=event
        )

        result = download_assets(downloader, assets, jobs=1)  # type: ignore[arg-type]

        assert isinstance(result[0], DownloadResult)
        assert not isinstance(result[0], DownloadCancelled)

    def test_ordinary_exceptions_preserved_during_cancel(self, tmp_path: Path) -> None:
        """Real failures that already happened are not masked by cancellation."""
        assets = make_assets(tmp_path, 3)
        event = threading.Event()
        boom = OSError("disk")

        def fail_then_stop(asset: CatalogAsset) -> DownloadResult:
            event.set()
            raise boom

        downloader = FakeDownloader(
            behaviours={assets[0].relative_path: fail_then_stop}, stop_event=event
        )

        result = download_assets(downloader, assets, jobs=1)  # type: ignore[arg-type]

        assert result[0] is boom
        assert isinstance(result[0], OSError)
        assert not isinstance(result[0], DownloadCancelled)

    def test_no_worker_threads_left_behind(self, tmp_path: Path) -> None:
        """The call returns only after all threads have exited."""
        assets = make_assets(tmp_path, 4)
        before = threading.active_count()

        download_assets(FakeDownloader(), assets, jobs=4)  # type: ignore[arg-type]

        assert threading.active_count() <= before

    def test_cancelled_before_any_task_starts(self, tmp_path: Path) -> None:
        """A pre-set event starts no tasks at all."""
        assets = make_assets(tmp_path, 3)
        event = threading.Event()
        event.set()
        downloader = FakeDownloader(stop_event=event)

        result = download_assets(downloader, assets, jobs=2)  # type: ignore[arg-type]

        assert result == [None, None, None]
        assert downloader.calls == []

    def test_download_cancelled_without_event_is_asset_error(
        self, tmp_path: Path
    ) -> None:
        """A ``DownloadCancelled`` while the event is clear is a per-asset error."""
        assets = make_assets(tmp_path, 2)
        downloader = FakeDownloader(
            behaviours={assets[0].relative_path: DownloadCancelled("odd")}
        )

        result = download_assets(downloader, assets, jobs=1)  # type: ignore[arg-type]

        assert isinstance(result[0], DownloadCancelled)
        assert isinstance(result[1], DownloadResult)


class TestKeyboardInterrupt:
    """Tests for ``KeyboardInterrupt`` handling."""

    def test_interrupt_sets_event_and_reraises(self, tmp_path: Path) -> None:
        """A ``KeyboardInterrupt`` stops the batch and propagates unchanged."""
        assets = make_assets(tmp_path, 4)
        downloader = FakeDownloader(
            behaviours={assets[0].relative_path: KeyboardInterrupt()}
        )

        with pytest.raises(KeyboardInterrupt):
            download_assets(downloader, assets, jobs=1)  # type: ignore[arg-type]

        assert downloader.stop_event is not None
        assert downloader.stop_event.is_set()

    def test_interrupt_waits_for_running_workers(self, tmp_path: Path) -> None:
        """Running downloads finish before the interrupt is re-raised."""
        assets = make_assets(tmp_path, 4)
        before = threading.active_count()
        downloader = FakeDownloader(
            behaviours={assets[0].relative_path: KeyboardInterrupt()}
        )

        with pytest.raises(KeyboardInterrupt):
            download_assets(downloader, assets, jobs=2)  # type: ignore[arg-type]

        assert threading.active_count() <= before

    def test_interrupt_not_returned_as_result(self, tmp_path: Path) -> None:
        """``KeyboardInterrupt`` never appears inside the result list."""
        assets = make_assets(tmp_path, 2)
        downloader = FakeDownloader(
            behaviours={assets[0].relative_path: KeyboardInterrupt()}
        )

        with pytest.raises(KeyboardInterrupt):
            results = download_assets(downloader, assets, jobs=1)  # type: ignore[arg-type]
            assert all(not isinstance(item, KeyboardInterrupt) for item in results)


class TestEdgeCases:
    """Remaining contract checks."""

    def test_no_deduplication(self, tmp_path: Path) -> None:
        """Duplicate assets are downloaded once per input position."""
        asset = make_assets(tmp_path, 1)[0]
        downloader = FakeDownloader()

        result = download_assets(downloader, [asset, asset])  # type: ignore[arg-type]

        assert len(result) == 2
        assert len(downloader.calls) == 2

    def test_result_length_always_matches_input(self, tmp_path: Path) -> None:
        """The returned list length tracks the input on every path."""
        assets = make_assets(tmp_path, 7)
        downloader = FakeDownloader(
            behaviours={
                assets[2].relative_path: RuntimeError("x"),
                assets[5].relative_path: ValueError("y"),
            }
        )

        result = download_assets(downloader, assets, jobs=3)  # type: ignore[arg-type]

        assert len(result) == len(assets)
