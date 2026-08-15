"""Shared pytest fixtures / markers for the psi-agent test suite."""

from __future__ import annotations

import sys

import pytest

# Unix domain sockets are unsupported by asyncio on Windows
# (``create_unix_server`` raises ``NotImplementedError``). Tests that stand up a
# real ``web.UnixSite`` / connect a ``ChannelCore`` over a ``.sock`` path can only
# run on POSIX. They still run in CI (Linux/macOS); on Windows they are skipped
# rather than reported as failures.
requires_unix_socket = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Unix domain sockets unsupported on Windows asyncio (runs on Linux/macOS CI)",
)
