"""WinBackend — Windows runtime for cua-driver.

Windows drives native apps through UIA (UI Automation) plus a synthetic cursor,
so screenshots, clicks, typing, scrolling and drags land on the target app
without moving the user's real cursor or stealing focus. The friendly action
surface is the same as macOS; the differences are the install/permission flow
(PowerShell one-liner + first-run input/screen authorization) and the capability
ledger below.

Capability ledger (``REFUSALS``)
--------------------------------
This maps ``action -> reason`` for anything the Windows cua-driver backend does
**not** support, so the tool refuses cleanly instead of shelling out to a driver
call that would fail with an opaque error. Per the official cua-driver account,
the documented action surface (capture / click family / type / key / scroll /
drag / focus_app / list_apps / wait / diagnostics) is supported on Windows, so
the ledger is currently empty. Add an entry here (not in ``base``/``mac``) if a
future cua-driver Windows build drops support for a specific action.
"""

from __future__ import annotations

from typing import ClassVar

from .base import INSTALL_DOC, Backend


class WinBackend(Backend):
    os_name = "Windows"

    #: Windows supports the documented action surface today; refuse nothing.
    #: Populate as ``{"<action>": "<reason shown to the user>"}`` if that changes.
    REFUSALS: ClassVar[dict[str, str]] = {}

    def install_hint(self) -> str:
        cmd = (
            "  # PowerShell (Windows x86_64 / ARM64):\n"
            "  irm https://cua.ai/driver/install.ps1 | iex\n"
            "  cua-driver check-permissions   # grant input/screen permissions if prompted"
        )
        return (
            f"Install cua-driver for {self.os_name} (guide: {INSTALL_DOC}):\n"
            f"{cmd}\n"
            "  cua-driver doctor              # verify the install"
        )
