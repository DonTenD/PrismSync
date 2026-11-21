"""Verify the public package surface."""

import prism_sync


def test_version_is_exposed() -> None:
    """The package exposes a non-empty version string."""
    assert prism_sync.__version__


def test_cli_entry_point_is_callable() -> None:
    """The CLI entry point lives in ``prism_sync.cli``, not the package root."""
    from prism_sync.cli import main

    assert callable(main)
    assert not hasattr(prism_sync, "main")
