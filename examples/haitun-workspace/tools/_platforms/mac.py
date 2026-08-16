"""MacBackend — macOS runtime for cua-driver.

macOS is the reference platform: cua-driver drives native apps through the
Accessibility (AX) tree plus synthetic input events on the private SkyLight
framework, so screenshots, clicks, typing, scrolling and drags land on the
target app **without moving the user's cursor, stealing keyboard focus, or
switching Spaces**. The full action surface is available here, so ``REFUSALS``
is empty — every friendly action maps straight through :meth:`Backend.dispatch`.
"""

from __future__ import annotations

from typing import ClassVar

from .base import INSTALL_DOC, Backend


class MacBackend(Backend):
    os_name = "macOS"

    # macOS supports the whole action surface — nothing is refused.
    REFUSALS: ClassVar[dict[str, str]] = {}

    def install_hint(self) -> str:
        cmd = (
            '  /bin/bash -c "$(curl -fsSL https://cua.ai/driver/install.sh)"\n'
            "  cua-driver permissions grant   # approve Accessibility + Screen Recording"
        )
        return (
            f"Install cua-driver for {self.os_name} (guide: {INSTALL_DOC}):\n"
            f"{cmd}\n"
            "  cua-driver doctor              # verify the install"
        )
