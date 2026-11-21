"""Local HTTP controlled integration tests for the sync pipeline.

These tests drive the real public API against a local HTTP server.  Nothing
here reaches the network, a gRPC service or real game data: they are
controlled integration tests, not CDN end-to-end tests.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from prism_sync import HttpClient, Selection, sync_if_needed
from prism_sync.downloader import PART_META_SUFFIX, PART_RAW_SUFFIX
from prism_sync.models import ResourceType, SyncOutcome
from prism_sync.state import SyncStateStore
from prism_sync.version import VERSION_MARKER_NAME, read_local_version

from ..helpers.catalog_builder import (
    SpecEntry,
    SyntheticCatalog,
    build_version,
    root_crc,
)
from ..helpers.local_http_server import LocalHttpServer, Route, serve

GAME_VERSION = "9.9.9"
SIMPLE = "12345678"
TYPES = ["catalog", "raw"]

ROOT_CHECKSUM = 0xA1A1
ROOT_SEED = 0
SUB_CHECKSUM = 0xB2B2
SUB_SEED = 0
ASSET_A_CHECKSUM = 0xC3C3
ASSET_B_CHECKSUM = 0xD4D4
SUB_ASSET_CHECKSUM = 0xE5E5

PAYLOAD_A = b"asset-a-payload" * 4
PAYLOAD_B = b"asset-b-payload" * 3
PAYLOAD_SUB = b"sub-asset-payload" * 5


def _entry(
    label: str,
    payload: bytes,
    checksum: int,
    *,
    type_name: str = "raw",
    resource_type: int = int(ResourceType.RawPlain),
) -> SpecEntry:
    return SpecEntry(
        label=label,
        type_name=type_name,
        resource_type=resource_type,
        size=len(payload),
        checksum=checksum,
    )


class Tree:
    """A two-catalog tree plus its downloadable payloads."""

    def __init__(self) -> None:
        self.builder = SyntheticCatalog(game_version=GAME_VERSION, simple=SIMPLE)
        self.payloads: dict[str, bytes] = {}
        self.root_labels: list[str] = []

    def build(self) -> tuple[str, dict[str, Route]]:
        sub_asset = _entry("sub-asset", PAYLOAD_SUB, SUB_ASSET_CHECKSUM)
        # The sub-catalog's own label CRC is what its entries resolve against,
        # and its stored size must match what the parent entry records.
        sub_label = SpecEntry(
            label="sub",
            type_name="catalog",
            resource_type=0,
            size=0,
            checksum=SUB_CHECKSUM,
            seed=SUB_SEED,
        )
        sub_name, sub_size = self.builder.add_catalog(
            checksum=SUB_CHECKSUM,
            label_crc=sub_label.label_crc(),
            seed=SUB_SEED,
            entries=[sub_asset],
            types=TYPES,
        )
        self.payloads[sub_asset.real_name()] = PAYLOAD_SUB

        asset_a = _entry("asset-a", PAYLOAD_A, ASSET_A_CHECKSUM)
        asset_b = _entry("asset-b", PAYLOAD_B, ASSET_B_CHECKSUM)
        self.payloads[asset_a.real_name()] = PAYLOAD_A
        self.payloads[asset_b.real_name()] = PAYLOAD_B

        children = [
            asset_a,
            asset_b,
            SpecEntry(
                label="sub",
                type_name="catalog",
                resource_type=0,
                size=sub_size,
                checksum=SUB_CHECKSUM,
                seed=SUB_SEED,
            ),
        ]
        root_name, root_size = self.builder.add_catalog(
            checksum=ROOT_CHECKSUM,
            label_crc=root_crc(GAME_VERSION, SIMPLE),
            seed=ROOT_SEED,
            entries=children,
            types=TYPES,
        )

        version = build_version(ROOT_CHECKSUM, root_size, ROOT_SEED, SIMPLE)

        routes: dict[str, Route] = {}
        for path, blob in self.builder.files.items():
            routes[path] = Route(body=blob)
        for real, payload in self.payloads.items():
            routes[f"/r/{real[:2]}/{real}"] = Route(body=payload)
        routes[f"/r/{root_name[:2]}/{root_name}"] = Route(
            body=self.builder.files[f"/r/{root_name[:2]}/{root_name}"]
        )
        # Silence unused-variable style checks for the sub catalog name.
        assert sub_name
        return version, routes


def _catalog_paths(server: LocalHttpServer) -> list[str]:
    """Return recorded request paths that are catalog objects (not assets)."""
    return [p for p in server.paths() if "/r/" not in p]


def _run(
    server: LocalHttpServer,
    output: Path,
    version: str,
    **kwargs: object,
) -> SyncOutcome:
    with HttpClient(base_url=server.base_url, retries=0) as client:
        return sync_if_needed(
            client,
            version,
            game_version=GAME_VERSION,
            output=output,
            **kwargs,  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------
# Scenario A: first full sync
# --------------------------------------------------------------------------


class TestFirstSync:
    def test_full_pipeline(self, tmp_path: Path) -> None:
        tree = Tree()
        version, routes = tree.build()
        output = tmp_path / "out"

        with serve(routes) as server:
            outcome = _run(server, output, version)

        # Catalog + assets were really fetched from the local server.
        assert _catalog_paths(server) == []
        assert server.count(lambda p: "/r/" in p) >= 4

        assert outcome.result is not None
        assert outcome.marker_written is True
        assert outcome.result.failed == 0
        assert outcome.result.downloaded == 3
        assert outcome.result.catalog_count == 2

        # Payloads match what the server served.
        for entry, payload in (
            ("asset-a", PAYLOAD_A),
            ("asset-b", PAYLOAD_B),
            ("sub-asset", PAYLOAD_SUB),
        ):
            written = output / "root" / f"{entry}.raw"
            if not written.is_file():
                written = output / "root" / "sub" / f"{entry}.raw"
            assert written.read_bytes() == payload, f"{entry} content mismatch"

        # Sub-catalog nesting is real: sub-asset lives one level deeper.
        assert (output / "root" / "sub" / "sub-asset.raw").is_file()

        # State snapshot.
        state = SyncStateStore(output / "sync-state.json").load()
        assert state["status"] == "OK"
        assert state["summary"]["downloaded"] == 3
        assert state["summary"]["catalogs"] == 2
        assert state["gameVersion"] == GAME_VERSION
        assert set(state["files"]) == {
            "root/asset-a.raw",
            "root/asset-b.raw",
            "root/sub/sub-asset.raw",
        }

        # Version marker.
        marker = read_local_version(output)
        assert marker is not None
        assert marker.resource_version == version
        assert marker.game_version == GAME_VERSION

        # No leftover part files.
        assert not list(output.rglob("*.part.raw"))
        assert not list(output.rglob("*.part.json"))

    def test_selection_limits_downloads(self, tmp_path: Path) -> None:
        tree = Tree()
        version, routes = tree.build()
        output = tmp_path / "out"

        with serve(routes) as server:
            outcome = _run(
                server, output, version, selection=Selection(include=("asset-a.*",))
            )

        assert outcome.result is not None
        assert outcome.result.downloaded == 1
        assert outcome.result.total_asset_count == 3
        assert (output / "root" / "asset-a.raw").is_file()
        assert not (output / "root" / "asset-b.raw").exists()


# --------------------------------------------------------------------------
# Scenario B: second run is up to date
# --------------------------------------------------------------------------


class TestSecondRun:
    def test_skips_everything(self, tmp_path: Path) -> None:
        tree = Tree()
        version, routes = tree.build()
        output = tmp_path / "out"

        with serve(routes) as server:
            first = _run(server, output, version)
            assert first.marker_written is True

            state_file = output / "sync-state.json"
            snapshot_before = state_file.read_bytes()
            marker_before = (output / VERSION_MARKER_NAME).read_bytes()
            server.clear()

            second = _run(server, output, version)

        assert second.result is None
        assert second.marker_written is False
        assert server.paths() == []
        assert state_file.read_bytes() == snapshot_before
        assert (output / VERSION_MARKER_NAME).read_bytes() == marker_before


# --------------------------------------------------------------------------
# Scenario C: force re-runs the sync
# --------------------------------------------------------------------------


class TestForce:
    def test_force_redownloads(self, tmp_path: Path) -> None:
        tree = Tree()
        version, routes = tree.build()
        output = tmp_path / "out"

        with serve(routes) as server:
            _run(server, output, version)
            server.clear()

            forced = _run(server, output, version, force=True)

        assert server.count(lambda p: "/r/" in p) >= 4
        assert forced.result is not None
        assert forced.marker_written is True
        # force bypasses the "file looks valid" shortcut, so every asset is
        # transferred again.
        assert forced.result.downloaded == 3
        assert forced.result.skipped == 0

    def test_force_uses_catalog_cache(self, tmp_path: Path) -> None:
        """``force`` re-downloads assets but does not refresh the catalog cache."""
        tree = Tree()
        version, routes = tree.build()
        output = tmp_path / "out"

        with serve(routes) as server:
            _run(server, output, version)
            cache_files = list((output / ".catalog-cache").glob("*.bin"))
            assert len(cache_files) == 2
            server.clear()

            _run(server, output, version, force=True)

        # Catalog cache hit means zero catalog object requests.
        assert all("/r/" in p for p in server.paths())


# --------------------------------------------------------------------------
# Scenario D: one asset fails
# --------------------------------------------------------------------------


class TestAssetFailure:
    def test_single_failure_keeps_others(self, tmp_path: Path) -> None:
        tree = Tree()
        version, routes = tree.build()
        output = tmp_path / "out"
        failing = f"/r/{tree.payloads and ''}"  # placeholder, replaced below

        broken_real = SpecEntry(
            label="asset-b",
            type_name="raw",
            resource_type=int(ResourceType.RawPlain),
            size=len(PAYLOAD_B),
            checksum=ASSET_B_CHECKSUM,
        ).real_name()
        failing = f"/r/{broken_real[:2]}/{broken_real}"
        routes[failing] = Route(body=b"boom", status=500)

        with serve(routes) as server:
            outcome = _run(server, output, version)

        assert outcome.result is not None
        assert outcome.result.failed == 1
        assert outcome.result.downloaded == 2
        # The original exception object is preserved.
        errors = [r for r in outcome.result.results if isinstance(r, Exception)]
        assert len(errors) == 1
        assert not outcome.marker_written
        assert not (output / VERSION_MARKER_NAME).exists()

        state = SyncStateStore(output / "sync-state.json").load()
        assert state["status"] == "PARTIAL"
        assert state["files"]["root/asset-b.raw"]["status"] == "failed"

    def test_failure_does_not_clobber_existing_marker(self, tmp_path: Path) -> None:
        tree = Tree()
        version, routes = tree.build()
        output = tmp_path / "out"

        with serve(routes) as server:
            _run(server, output, version)
            marker_before = (output / VERSION_MARKER_NAME).read_bytes()

            broken_real = SpecEntry(
                label="asset-a",
                type_name="raw",
                resource_type=int(ResourceType.RawPlain),
                size=len(PAYLOAD_A),
                checksum=ASSET_A_CHECKSUM,
            ).real_name()
            routes[f"/r/{broken_real[:2]}/{broken_real}"] = Route(
                body=b"boom", status=500
            )
            server.set_route(
                f"/r/{broken_real[:2]}/{broken_real}",
                Route(body=b"boom", status=500),
            )

            forced = _run(server, output, version, force=True)

        assert forced.result is not None
        assert forced.result.failed == 1
        assert not forced.marker_written
        assert (output / VERSION_MARKER_NAME).read_bytes() == marker_before


# --------------------------------------------------------------------------
# Scenario E: cancellation and resume
# --------------------------------------------------------------------------


class TestCancellation:
    def test_cancel_then_resume(self, tmp_path: Path) -> None:
        tree = Tree()
        version, routes = tree.build()
        output = tmp_path / "out"

        gate = threading.Event()
        slow_real = SpecEntry(
            label="asset-a",
            type_name="raw",
            resource_type=int(ResourceType.RawPlain),
            size=len(PAYLOAD_A),
            checksum=ASSET_A_CHECKSUM,
        ).real_name()
        slow_path = f"/r/{slow_real[:2]}/{slow_real}"
        routes[slow_path] = Route(body=PAYLOAD_A, delay_gate=gate)

        stop = threading.Event()
        outcome_holder: dict[str, object] = {}

        with serve(routes) as server:
            # The server blocks the slow response on ``gate``; a watcher thread
            # flips ``stop`` as soon as that request arrives, then releases the
            # handler so the worker thread can observe the cancellation.
            def watcher() -> None:
                for _ in range(500):
                    if any(p == slow_path for p in server.paths()):
                        stop.set()
                        gate.set()
                        return
                    threading.Event().wait(0.01)
                gate.set()

            watcher_thread = threading.Thread(target=watcher, daemon=True)
            watcher_thread.start()

            runner = threading.Thread(
                target=lambda: outcome_holder.update(
                    outcome=_run(
                        server,
                        output,
                        version,
                        stop_event=stop,
                        jobs=1,
                        selection=Selection(include=("asset-a.*",)),
                    )
                ),
                daemon=True,
            )
            runner.start()
            runner.join(timeout=30)
            watcher_thread.join(timeout=5)
            gate.set()

        assert not runner.is_alive(), "sync hung"
        assert slow_path in server.paths(), "slow asset never requested"

        cancelled = outcome_holder["outcome"]
        assert isinstance(cancelled, SyncOutcome)
        assert cancelled.result is not None
        assert not cancelled.marker_written
        assert not (output / VERSION_MARKER_NAME).exists()
        assert cancelled.result.failed == 0

        # Resuming with a clean event completes the download.
        routes[slow_path] = Route(body=PAYLOAD_A)
        with serve(routes) as server2:
            resumed = _run(
                server2,
                output,
                version,
                jobs=1,
                selection=Selection(include=("asset-a.*",)),
            )

        assert resumed.result is not None
        assert resumed.marker_written is True
        assert (output / "root" / "asset-a.raw").read_bytes() == PAYLOAD_A
        assert not list(output.rglob(f"*{PART_RAW_SUFFIX}"))
        assert not list(output.rglob(f"*{PART_META_SUFFIX}"))


# --------------------------------------------------------------------------
# Sanity: the served bytes really are production-formatted catalogs
# --------------------------------------------------------------------------


def test_served_catalog_roundtrips_through_parser(tmp_path: Path) -> None:
    """Guard the fixture itself: it must decode with the production parser."""
    from prism_sync.catalog import decode_catalog, parse_catalog

    builder = SyntheticCatalog(game_version=GAME_VERSION, simple=SIMPLE)
    entries = [_entry("only", b"data", 0x7777)]
    name, size = builder.add_catalog(
        checksum=0x9999, label_crc=0x1111, seed=0, entries=entries, types=TYPES
    )
    blob = builder.files[f"/r/{name[:2]}/{name}"]

    plain = decode_catalog(0, 0x1111, size, blob)
    parsed = parse_catalog(plain)

    assert len(parsed) == 1
    assert parsed[0].label == "only"
    assert parsed[0].size == 4
    assert hashlib.sha256(blob).hexdigest()  # non-empty fixture
