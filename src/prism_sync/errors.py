"""Domain error base classes."""

from __future__ import annotations


class CatalogError(Exception):
    """Base class for catalog domain errors."""


class CatalogTreeError(CatalogError):
    """Raised when the catalog tree is unsafe or inconsistent."""


class DownloadCancelled(CatalogError):
    """The caller asked the download to stop; partial files are kept."""
