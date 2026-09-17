"""The one exception type this sync raises."""

from __future__ import annotations


class SyncError(RuntimeError):
    """Raised for any non-recoverable discovery/apply failure; message is safe to print."""
