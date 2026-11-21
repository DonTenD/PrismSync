"""Domain models for resource synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .constants import DEFAULT_BASE_URL, DEFAULT_GRPC_TARGET
from .manifest import real_name

if TYPE_CHECKING:
    from .downloader import DownloadResult


class ResourceType(IntEnum):
    """Numeric resource type stored in a catalog entry."""

    BundlePlain = 0
    BundlePaddedStart = 1
    RawPlain = 64
    RawLz4Compressed = 65
    RawEncrypted = 66

    @classmethod
    def from_value(cls, value: int) -> ResourceType:
        """Create a ResourceType from a numeric value."""
        try:
            return cls(value)
        except ValueError as exc:
            raise UnsupportedResourceType(value) from exc


class UnsupportedResourceType(NotImplementedError):
    """Raised when a catalog uses a resource type this version does not know."""

    def __init__(self, value: int) -> None:
        """Initialize the exception with the unsupported resource type value."""
        self.value = value
        super().__init__(f"unsupported resource type: {value}")


@dataclass(frozen=True, slots=True)
class CatalogManifest:
    """Identity of a catalog object on the CDN."""

    label_crc: int
    size: int
    checksum: int
    seed: int

    def real_name(self) -> str:
        """Return the CDN object name for this catalog manifest."""
        return real_name(self.checksum, self.size, self.label_crc)


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One entry from a parsed catalog.

    ``resource_type`` is ``None`` when the catalog contains a numeric type this
    build does not know.  The raw value is preserved in
    ``resource_type_value`` for diagnostics.
    """

    label: str
    type_name: str
    size: int
    checksum: int
    seed: int
    label_crc: int
    priority: int
    resource_type: ResourceType | None = None
    resource_type_value: int = 0

    def as_manifest(self) -> CatalogManifest:
        """Convert this entry to a CatalogManifest."""
        return CatalogManifest(
            label_crc=self.label_crc,
            size=self.size,
            checksum=self.checksum,
            seed=self.seed,
        )

    @property
    def resource_type_name(self) -> str:
        """Return the resource type name or 'Unknown(N)' for unknown types."""
        if self.resource_type is None:
            return f"Unknown({self.resource_type_value})"
        return self.resource_type.name


@dataclass(frozen=True, slots=True)
class AssetInfoEntry:
    """JSON-compatible ``assetinfo.json`` entry."""

    name: str
    path: str
    type_name: str

    def to_dict(self) -> dict[str, str]:
        """Convert to dictionary matching the C# ReadableCatalogEntry format."""
        # Field names intentionally match the C# record:
        # ReadableCatalogEntry(string Name, string Path, ResourceType Type)
        return {"Name": self.name, "Path": self.path, "Type": self.type_name}


@dataclass(frozen=True, slots=True)
class CatalogAsset:
    """A downloadable (non-catalog) asset with its resolved output path."""

    entry: CatalogEntry
    catalog_label: str
    output_path: str
    relative_path: str
    real_name: str


@dataclass(frozen=True, slots=True)
class VersionProfile:
    """Application identity required by the GetVersion gRPC call."""

    game_version: str
    version_hash: str
    target: str = DEFAULT_GRPC_TARGET
    resource_target: str = "production"
    application_platform: str = "google"
    os_name: str = "android"

    def metadata(self, resource_version: str = "") -> list[tuple[str, str]]:
        """Return gRPC metadata tuples for the GetVersion call."""
        return [
            ("x-application-platform", self.application_platform),
            ("x-os-name", self.os_name),
            ("x-application-version", self.game_version),
            ("x-version-hash", self.version_hash),
            ("x-resource-target", self.resource_target),
            ("x-resource-version", resource_version),
        ]


@dataclass(frozen=True, slots=True)
class LocalVersion:
    """Local version information stored in .prism-version.json."""

    game_version: str
    resource_version: str
    catalog_fingerprint: str | None = None
    synced_at: str | None = None
    selection: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class VersionCheck:
    """Result of comparing local and remote resource versions."""

    game_version: str
    remote_resource_version: str
    remote_simple: str
    local: LocalVersion | None
    up_to_date: bool
    reason: str


MAX_JOBS = 16


@dataclass(slots=True)
class SyncOptions:
    """Options for catalog synchronization."""

    game_version: str
    resource_version: str
    output: Path
    jobs: int = 8
    force: bool = False
    catalog_only: bool = False
    prune: bool = False
    no_localizetext: bool = False
    verify: bool = False
    deep: bool = False
    resume: bool = True
    dry_run: bool = False
    stats_only: bool = False
    catalog_cache: bool = True
    refresh_catalogs: bool = False
    timeout: float = 60.0
    retries: int = 3
    base_url: str = DEFAULT_BASE_URL
    trust_env: bool = True

    def validate(self) -> None:
        """Validate sync options."""
        if not 1 <= self.jobs <= MAX_JOBS:
            raise ValueError(f"jobs must be between 1 and {MAX_JOBS}")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.retries < 0:
            raise ValueError("retries must be non-negative")
        if self.prune and self.catalog_only:
            raise ValueError("prune cannot be combined with catalog_only")
        if self.dry_run and self.stats_only:
            raise ValueError("dry_run and stats_only are mutually exclusive")
        if self.dry_run and self.catalog_only:
            raise ValueError("dry_run cannot be combined with catalog_only")
        if self.stats_only and self.catalog_only:
            raise ValueError("stats_only cannot be combined with catalog_only")


@dataclass(slots=True)
class SyncResult:
    """Outcome of one :func:`~prism_sync.sync.sync_resources` call.

    Attributes:
        assets: Assets considered for this run, in discovery order.
        results: One entry per asset from ``assets``, aligned by position:
            a ``DownloadResult`` for a downloaded or skipped asset, the
            original ``Exception`` for a failed asset, or ``None`` when the
            asset never started because the batch was cancelled.
        downloaded: Number of assets actually transferred.
        skipped: Number of assets whose local file was already valid.
        failed: Number of assets that raised a non-cancellation error.
        resumed: Number of downloads that continued a partial transfer.
        catalog_fingerprint: Fingerprint of the whole catalog tree.
        catalog_count: Number of catalogs discovered.
        total_asset_count: Number of assets before selection filtering.
        state: The snapshot written to the state file, or ``None`` when the
            batch was cancelled or the caller asked for no write.
    """

    assets: tuple[CatalogAsset, ...] = ()
    results: tuple[DownloadResult | Exception | None, ...] = ()
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    resumed: int = 0
    catalog_fingerprint: str = ""
    catalog_count: int = 0
    total_asset_count: int = 0
    state: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    """Result of one :func:`~prism_sync.sync.sync_if_needed` call.

    Attributes:
        check: The version comparison that drove the decision.
        result: The synchronization result, or ``None`` when the local copy was
            already up to date and nothing was downloaded.
        marker_written: Whether the local version marker was updated.  It stays
            ``False`` whenever the sync was skipped, incomplete, cancelled or
            the marker write raised.
    """

    check: VersionCheck
    result: SyncResult | None
    marker_written: bool
