"""HTTP-backed catalog fetching and caching."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from ._fsutil import write_json_atomic
from .catalog import decode_catalog, parse_catalog
from .crc import root_label_crc
from .errors import CatalogTreeError
from .http_client import BytesResponse, HttpClient, build_asset_url
from .manifest import decode_full_version, real_name
from .models import AssetInfoEntry, CatalogAsset, CatalogEntry, CatalogManifest

LOGGER = logging.getLogger(__name__)

MAX_RECURSION_DEPTH = 32


class HttpCatalogClientError(Exception):
    """Base class for HTTP catalog client errors."""


_DRIVE_PART = re.compile(r"^[A-Za-z]:$")
_INVALID_WINDOWS_CHARS = set('<>:"|?*')
_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _validate_path_part(part: str) -> None:
    if part in ("", ".", ".."):
        raise CatalogTreeError(f"unsafe path component: {part!r}")
    if "\\" in part or "\x00" in part:
        raise CatalogTreeError(f"unsafe path component: {part!r}")
    if _DRIVE_PART.match(part):
        raise CatalogTreeError(f"unsafe drive component: {part!r}")
    if any(char in _INVALID_WINDOWS_CHARS for char in part):
        raise CatalogTreeError(f"unsafe Windows character in path component: {part!r}")
    if part.split(".", 1)[0].upper() in _RESERVED_WINDOWS_NAMES:
        raise CatalogTreeError(f"reserved Windows device name: {part!r}")


def sanitize_relative(name: str) -> Path:
    """Sanitize a relative path for safe filesystem operations."""
    if not name:
        raise CatalogTreeError("empty catalog path")
    pure = PurePosixPath(name)
    if pure.is_absolute():
        raise CatalogTreeError(f"absolute catalog path is not allowed: {name!r}")
    parts = pure.parts
    if not parts:
        raise CatalogTreeError(f"empty catalog path: {name!r}")
    for part in parts:
        _validate_path_part(part)
    return Path(*parts)


def catalog_entry_to_asset(
    entry: CatalogEntry,
    *,
    catalog_label: str,
    output_dir: Path,
    root_output: Path,
) -> CatalogAsset:
    """Build the downloadable asset for one catalog entry.

    The asset's path is ``output_dir / <label>.<type>`` resolved and
    checked against ``root_output`` so a crafted label cannot escape the
    output directory.  ``relative_path`` is reported relative to
    ``root_output`` in POSIX form, matching the state file and
    ``AssetDownloader`` keying.

    Only entries that are themselves downloadable should be passed; catalog
    entries are discovered and recursed into by the caller.

    Args:
        entry: Parsed catalog entry to convert.
        catalog_label: Label of the catalog that contains ``entry``.
        output_dir: Directory of that catalog's assets, usually
        ``root_output / <catalog_label>``.
        root_output: Root output directory the asset must stay inside.

    Returns:
        The corresponding ``CatalogAsset``.

    Raises:
        CatalogTreeError: If the entry name is unsafe, or the resolved path
            escapes ``root_output``.
    """
    name = f"{entry.label}.{entry.type_name}"
    asset_path = (output_dir / sanitize_relative(name)).resolve()
    if not asset_path.is_relative_to(root_output):
        raise CatalogTreeError(f"asset path escapes output directory: {name!r}")
    return CatalogAsset(
        entry=entry,
        catalog_label=catalog_label,
        output_path=str(asset_path),
        relative_path=asset_path.relative_to(root_output).as_posix(),
        real_name=real_name(entry.checksum, entry.size, entry.label_crc),
    )


def catalog_fingerprint(assetinfo: dict[str, list[AssetInfoEntry]]) -> str:
    """Return the stable fingerprint of a catalog tree.

    The fingerprint is ``sha256`` over the catalog ``assetinfo`` structure
    serialised exactly like ``assetinfo.json``: UTF-8, no ASCII escaping,
    ``sort_keys=True`` and compact separators.  Only the mapping keys are
    sorted, so the order of the entries inside each catalog is significant.

    Args:
        assetinfo: Catalog label to entry list.

    Returns:
        Lowercase hexadecimal SHA-256 digest.
    """
    payload = json.dumps(
        {
            label: [entry.to_dict() for entry in entries]
            for label, entries in assetinfo.items()
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _discover_catalog_tree(
    client: HttpClient,
    version: str,
    *,
    game_version: str,
    output: Path,
    use_cache: bool,
    refresh: bool,
    writable: bool,
) -> tuple[list[CatalogAsset], dict[str, list[AssetInfoEntry]]]:
    """Walk the catalog tree once and return its assets and assetinfo."""
    full = decode_full_version(version)
    root_manifest = CatalogManifest(
        label_crc=root_label_crc(game_version, full.simple),
        size=full.size,
        checksum=full.checksum,
        seed=full.seed,
    )

    output_root = output.resolve()
    assets: list[CatalogAsset] = []
    catalogs: dict[str, list[AssetInfoEntry]] = {}
    visited: set[str] = set()
    stack: list[tuple[str, CatalogManifest, Path, int]] = [
        ("root", root_manifest, output_root / "root", 0)
    ]

    while stack:
        label, manifest, out_dir, depth = stack.pop()
        if depth > MAX_RECURSION_DEPTH:
            raise CatalogTreeError(
                f"catalog recursion exceeds {MAX_RECURSION_DEPTH} levels at {label!r}"
            )
        if label in catalogs:
            raise CatalogTreeError(f"duplicate catalog label: {label!r}")

        real = manifest.real_name()
        if real in visited:
            raise CatalogTreeError(
                f"catalog {label!r} points at an already visited object: {real}"
            )
        visited.add(real)

        LOGGER.info("catalog %s: %s", label, real)
        entries = load_catalog(
            client,
            manifest,
            output_root,
            use_cache=use_cache,
            refresh=refresh,
            writable=writable,
        )

        info_entries: list[AssetInfoEntry] = []
        subcatalogs: list[CatalogEntry] = []

        for entry in entries:
            name = f"{entry.label}.{entry.type_name}"
            info_entries.append(
                AssetInfoEntry(
                    name=name,
                    path=real_name(entry.checksum, entry.size, entry.label_crc),
                    type_name=entry.resource_type_name,
                )
            )

            if entry.type_name == "catalog":
                subcatalogs.append(entry)
                continue

            assets.append(
                catalog_entry_to_asset(
                    entry,
                    catalog_label=label,
                    output_dir=out_dir,
                    root_output=output_root,
                )
            )

        catalogs[label] = info_entries

        for entry in reversed(subcatalogs):
            sub_dir = (out_dir / sanitize_relative(entry.label)).resolve()
            if not sub_dir.is_relative_to(output_root):
                raise CatalogTreeError(
                    f"catalog path escapes output directory: {entry.label!r}"
                )
            stack.append((entry.label, entry.as_manifest(), sub_dir, depth + 1))

    return assets, catalogs


def discover_catalog_assets(
    client: HttpClient,
    version: str,
    *,
    game_version: str,
    output: Path,
    use_cache: bool = True,
    refresh: bool = False,
    writable: bool = True,
) -> list[CatalogAsset]:
    """Discover every downloadable asset reachable from ``version``.

    The root catalog is derived from ``version`` and ``game_version``, then
    each catalog is loaded, parsed and walked.  Entries whose ``type_name`` is
    ``"catalog"`` are pushed back as nested catalogs instead of becoming
    assets, so the returned list never contains a catalog object itself.

    Traversal mirrors the original implementation: a LIFO stack with the
    sub-catalogs pushed in reverse, which yields a depth-first walk where the
    first sub-catalog is fully expanded before its siblings.

    Args:
        client: HTTP client used for catalog requests.
        version: ``"<simple>@<base64>"`` resource version.
        game_version: Game version used to derive the root label CRC.
        output: Resource output root.  It is resolved once and every asset and
            sub-catalog path is checked against the resolved directory.
        use_cache: Read catalogs from ``<output>/.catalog-cache``.
        refresh: Revalidate cached catalogs with the CDN.
        writable: Allow writing the catalog cache.

    Returns:
        Every non-catalog asset in depth-first discovery order.

    Raises:
        CatalogTreeError: If a catalog label repeats, a catalog object is
            reached twice, the recursion nesting exceeds
            ``MAX_RECURSION_DEPTH``, or a path escapes ``output``.
        CatalogFormatError: If a catalog payload is malformed.
        ValueError: If ``version`` cannot be decoded.
        DownloadError: If a catalog cannot be fetched.
    """
    assets, _assetinfo = _discover_catalog_tree(
        client,
        version,
        game_version=game_version,
        output=output,
        use_cache=use_cache,
        refresh=refresh,
        writable=writable,
    )
    return assets


def discover_catalog_tree(
    client: HttpClient,
    version: str,
    *,
    game_version: str,
    output: Path,
    use_cache: bool = True,
    refresh: bool = False,
    writable: bool = True,
) -> tuple[list[CatalogAsset], dict[str, list[AssetInfoEntry]]]:
    """Discover the catalog tree and also return its ``assetinfo`` mapping.

    Behaves exactly like :func:`discover_catalog_assets`; the extra return
    value holds one :class:`AssetInfoEntry` per catalog entry, including the
    sub-catalog entries themselves, keyed by catalog label in discovery order.
    Feed it to :func:`catalog_fingerprint` to obtain the tree fingerprint that
    the sync state and version marker record.

    Args:
        client: HTTP client used for catalog requests.
        version: ``"<simple>@<base64>"`` resource version.
        game_version: Game version used to derive the root label CRC.
        output: Resource output root, resolved once before any path check.
        use_cache: Read catalogs from ``<output>/.catalog-cache``.
        refresh: Revalidate cached catalogs with the CDN.
        writable: Allow writing the catalog cache.

    Returns:
        A ``(assets, assetinfo)`` pair.  ``assets`` never contains a catalog
        entry; ``assetinfo`` does.

    Raises:
        CatalogTreeError: If a catalog label repeats, a catalog object is
            reached twice, the recursion nesting exceeds
            ``MAX_RECURSION_DEPTH``, or a path escapes ``output``.
        CatalogFormatError: If a catalog payload is malformed.
        ValueError: If ``version`` cannot be decoded.
        DownloadError: If a catalog cannot be fetched.
    """
    return _discover_catalog_tree(
        client,
        version,
        game_version=game_version,
        output=output,
        use_cache=use_cache,
        refresh=refresh,
        writable=writable,
    )


def _catalog_cache_paths(output: str | Path, real: str) -> tuple[Path, Path]:
    """Get the cache file paths for a catalog entry."""
    base = Path(output) / ".catalog-cache"
    return base / f"{real}.bin", base / f"{real}.json"


def _read_catalog_cache(path: Path, manifest: CatalogManifest) -> bytes | None:
    """Read cached catalog data if it exists and is valid."""
    if path.is_symlink():
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    if stat.st_size != manifest.size:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _read_catalog_meta(path: Path) -> dict[str, Any] | None:
    """Read catalog cache metadata."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_catalog_cache(
    path: Path,
    meta_path: Path,
    blob: bytes,
    headers: httpx.Headers,
) -> None:
    """Write catalog data and metadata to cache atomically."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(blob)
        tmp.replace(path)
        write_json_atomic(
            meta_path,
            {
                "etag": headers.get("etag"),
                "lastModified": headers.get("last-modified"),
                "size": len(blob),
                "cachedAt": datetime.now(UTC).isoformat(),
            },
        )
    except OSError as exc:
        LOGGER.warning("cannot write catalog cache %s: %s", path, exc)
        with contextlib.suppress(OSError):
            tmp.unlink()


def _delete_catalog_cache(path: Path, meta_path: Path) -> None:
    """Delete catalog cache files."""
    for item in (path, meta_path):
        try:
            item.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            LOGGER.warning("cannot remove catalog cache %s: %s", item, exc)


def _read_or_fetch_catalog(
    client: HttpClient,
    real: str,
    manifest: CatalogManifest,
    cache_path: Path,
    meta_path: Path,
    *,
    use_cache: bool = True,
    refresh: bool = False,
    writable: bool = True,
) -> tuple[bytes, bool]:
    """Read catalog from cache or fetch from HTTP.

    Returns:
        Tuple of (catalog bytes, from_cache).
    """
    if use_cache and not refresh:
        cached = _read_catalog_cache(cache_path, manifest)
        if cached is not None:
            LOGGER.debug("catalog cache hit: %s", real)
            return cached, True

    url = build_asset_url(real, client.base_url)
    headers: dict[str, str] | None = None
    meta = _read_catalog_meta(meta_path) if use_cache else None

    if refresh and meta is not None and isinstance(meta.get("etag"), str):
        headers = {"If-None-Match": meta["etag"]}

    response: BytesResponse = client.get_bytes_response(url, headers=headers)
    content = response.content

    if content is None:
        # 304 Not Modified
        cached = _read_catalog_cache(cache_path, manifest) if use_cache else None
        if cached is not None:
            LOGGER.debug("catalog not modified: %s", real)
            return cached, True
        # Re-fetch without conditional headers
        response = client.get_bytes_response(url)
        content = response.content
        if content is None:
            raise CatalogTreeError(f"catalog {real} returned 304 without a cache")

    if writable:
        _write_catalog_cache(cache_path, meta_path, content, response.headers)

    return content, False


def fetch_catalog(
    client: HttpClient,
    manifest: CatalogManifest,
    output: str | Path,
    *,
    use_cache: bool = True,
    refresh: bool = False,
    writable: bool = True,
) -> bytes:
    """Fetch and return the raw catalog bytes for a manifest.

    Args:
        client: HTTP client for making requests.
        manifest: The catalog manifest.
        output: Output directory for cache files.
        use_cache: Whether to use cached catalogs.
        refresh: Whether to refresh the cache.
        writable: Whether to write to the cache.

    Returns:
        Raw catalog bytes (encrypted and compressed).
    """
    real = manifest.real_name()
    cache_path, meta_path = _catalog_cache_paths(output, real)

    blob, _from_cache = _read_or_fetch_catalog(
        client,
        real,
        manifest,
        cache_path,
        meta_path,
        use_cache=use_cache,
        refresh=refresh,
        writable=writable and use_cache,
    )

    return blob


def load_catalog(
    client: HttpClient,
    manifest: CatalogManifest,
    output: str | Path,
    *,
    use_cache: bool = True,
    refresh: bool = False,
    writable: bool = True,
) -> list[CatalogEntry]:
    """Fetch, decrypt, decompress, and parse a catalog.

    Args:
        client: HTTP client for making requests.
        manifest: The catalog manifest.
        output: Output directory for cache files.
        use_cache: Whether to use cached catalogs.
        refresh: Whether to refresh the cache.
        writable: Whether to write to the cache.

    Returns:
        List of parsed catalog entries.
    """
    real = manifest.real_name()
    cache_path, meta_path = _catalog_cache_paths(output, real)

    blob, _from_cache = _read_or_fetch_catalog(
        client,
        real,
        manifest,
        cache_path,
        meta_path,
        use_cache=use_cache,
        refresh=refresh,
        writable=writable and use_cache,
    )

    try:
        plain = decode_catalog(
            manifest.seed,
            manifest.label_crc,
            manifest.size,
            blob,
        )
    except Exception:
        if not _from_cache:
            raise
        LOGGER.warning("catalog cache is corrupt; refetching %s", real)
        _delete_catalog_cache(cache_path, meta_path)
        # Re-fetch without cache
        response = client.get_bytes_response(build_asset_url(real, client.base_url))
        content = response.content
        if content is None:
            raise
        if writable and use_cache:
            _write_catalog_cache(cache_path, meta_path, content, response.headers)
        plain = decode_catalog(
            manifest.seed,
            manifest.label_crc,
            manifest.size,
            content,
        )

    return parse_catalog(plain)
