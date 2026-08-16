"""Private runtime package for the ``computer_use`` tool.

Not a tool itself: the tool registry globs ``*.py`` non-recursively and skips
``_``-prefixed names, so this ``_platforms`` package is never scanned or exposed
as a tool. ``computer_use.py`` (the thin dispatcher) is the only public surface.

Platform selector
------------------
:func:`get_backend` maps ``sys.platform`` to a cached backend singleton:

    darwin  -> MacBackend   (full action surface, no refusals)
    win32   -> WinBackend   (UIA + synthetic cursor; capability ledger in win.py)

Any other platform (notably Linux desktop, which is out of scope) raises a clear
:class:`RuntimeError` rather than silently degrading.
"""

from __future__ import annotations

import sys

from .base import INSTALL_DOC, Backend
from .mac import MacBackend
from .win import WinBackend

__all__ = ["INSTALL_DOC", "Backend", "get_backend"]

# Cached per-process backend singleton (keyed by the resolved platform).
_backend: Backend | None = None


def get_backend() -> Backend:
    """Return the cua-driver backend for the current OS (cached singleton).

    Raises:
        RuntimeError: on an unsupported platform (e.g. Linux desktop), with a
            message naming the platform and the supported set.
    """
    global _backend
    if _backend is not None:
        return _backend

    if sys.platform == "darwin":
        _backend = MacBackend()
    elif sys.platform.startswith("win"):
        _backend = WinBackend()
    else:
        raise RuntimeError(
            f"computer_use: unsupported platform {sys.platform!r}. "
            "cua-driver is wired here for macOS (darwin) and Windows (win32) only; "
            "Linux desktop is out of scope. "
            f"See the install guide: {INSTALL_DOC}"
        )
    return _backend
