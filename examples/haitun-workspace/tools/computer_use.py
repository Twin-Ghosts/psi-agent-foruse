"""computer_use tool - drive the desktop in the background via ``cua-driver``.

Cross-platform: ``cua-driver`` supports **macOS and Windows** (same CLI, same
action surface). It drives native apps through each OS's accessibility / input
layer — on macOS via the Accessibility (AX) tree + the private SkyLight
framework; on Windows via Win32/UIA with synthetic cursors — so screenshots,
clicks, typing, scrolling and drags land on a target app **without moving the
user's cursor, stealing keyboard focus, or switching Spaces/desktops**. Works
with any tool-capable model.

This file is a **thin dispatcher**: the public ``computer_use(...)`` action
surface below is unchanged, but the runtime lives in the private ``_platforms``
package. ``_platforms.get_backend()`` picks a backend by ``sys.platform``
(``MacBackend`` / ``WinBackend``) and the friendly action is forwarded verbatim
to ``Backend.dispatch``. The registry never scans ``_platforms`` (non-recursive
``*.py`` glob that also skips ``_`` names), so it stays a private runtime, not a
second tool.

Note: this tool operates the desktop of **the machine cua-driver runs on** (i.e.
the machine hosting this agent). To drive a *different* user's computer, see
``computer_use_remote`` (HTTP → agent on the user's box). For a dependency-light
local alternative that uses ``pyautogui`` instead of cua-driver, see
``computer_use_local``.

``cua-driver`` is an external app + CLI (installed via the one-line installer,
not a Python package), so the backend shells out to it with
:func:`anyio.run_process` rather than importing a library — no extra dependency
is added. Every drive action maps to ``cua-driver call <tool> '<json>'``, the
same handler the driver's MCP server uses; diagnostic actions use the driver's
own subcommands (``doctor``, ``permissions``, ``list-tools``, ``describe``).

Screenshots come back as PNG bytes; the backend writes them under
``generated/computer_use/`` and returns the absolute path so the caller can
deliver it with a ``MEDIA:`` / ``[SEND:]`` marker.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The private runtime package (_platforms) sits next to this file. Tool files are
# exec'd as standalone modules, so make the tools dir importable, then import the
# platform selector. Mirrors the _feishu / _c_drive_cleanup_impl pattern.
_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from _platforms import get_backend  # noqa: E402

# Back-compat: some callers/tests reference the module-level install hint string.
# Resolve it lazily-ish from the current platform's backend where possible; fall
# back to the shared doc pointer if the platform is unsupported here.
try:
    _INSTALL_HINT = get_backend().install_hint()
except Exception:  # unsupported platform — keep import side effects harmless
    from _platforms import INSTALL_DOC

    _INSTALL_HINT = f"See the official install guide: {INSTALL_DOC}"


async def computer_use(
    action: str = "capture",
    app: str = "",
    mode: str = "som",
    tool: str = "",
    args: str = "",
    element: int | None = None,
    coordinate: list[int] | None = None,
    text: str = "",
    keys: str = "",
    direction: str = "",
    amount: int = 0,
    from_element: int | None = None,
    to_element: int | None = None,
    from_coordinate: list[int] | None = None,
    to_coordinate: list[int] | None = None,
    modifiers: list[str] | None = None,
    raise_window: bool = False,
    seconds: float = 0.0,
    capture_after: bool = False,
) -> str:
    """Drive the desktop (macOS/Windows) in the background through ``cua-driver``.

    Captures and input events target a specific app via its accessibility tree
    and do NOT move the user's cursor, steal keyboard focus, or switch Spaces/
    desktops. Typical loop: ``capture`` (mode="som" for a screenshot with numbered
    element overlays + AX index) → act by ``element`` index → re-``capture`` to
    verify (or pass ``capture_after=True`` to fold the follow-up screenshot in).

    Actions:
      - capture: screenshot the desktop/app. mode="som" (screenshot+overlays+AX
        index, default), "vision" (plain screenshot), "ax" (AX tree text only,
        no image). Scope with ``app``. PNG is saved under generated/computer_use/.
      - click / double_click / right_click / middle_click: target by ``element``
        index (preferred) or ``coordinate`` [x, y]; optional ``modifiers``.
      - type: enter ``text``.  key: press ``keys`` (e.g. "cmd+s", "return").
      - scroll: ``direction`` up/down/left/right, ``amount``, at ``element``/``coordinate``.
      - drag: from ``from_element``/``from_coordinate`` to ``to_element``/``to_coordinate``.
      - focus_app / list_apps: focus (``raise_window`` stays False unless asked) or enumerate apps.
      - browser_* (drive WEB-PAGE elements via CDP; needs the browser started with a
        remote-debugging port — gateway ``--browser-debug-port``):
          browser_prepare (attach a browser pid; pass ``args='{"pid":1234}'`` or
            ``args='{"allow_launch":true,...}'``), get_browser_state (read tabs/refs),
          browser_navigate (``args='{"url":"..."}'``),
          browser_click (click a page ref ``args='{"ref":"..."}'`` or viewport ``coordinate``),
          browser_type (``text`` into a ref), browser_pointer (hover/scroll/drag).
        Only ``coordinate``/``text`` are auto-passed; everything else (pid/ref/url) via ``args``.
      - wait: sleep ``seconds``.
      - call: escape hatch — invoke MCP ``tool`` with raw JSON ``args`` verbatim.
      - list_tools / describe (``tool``) / doctor / permissions / version / setup: diagnostics.

    Underlying tool names/schemas can vary by cua-driver version; if a drive
    action is rejected, run action="list_tools" / action="describe" to see the
    exact schema, then use action="call" with ``tool`` + ``args`` to match it.

    Args:
        action: What to do (see list above). Defaults to "capture".
        app: App name/bundle id to scope a capture or focus to.
        mode: Capture mode: "som" (default), "vision", or "ax".
        tool: MCP tool name for action="call"/"describe" (overrides the default mapping).
        args: Raw JSON object merged into the call payload (wins on key clashes).
        element: Element index (from a "som"/"ax" capture) to target.
        coordinate: Pixel [x, y] fallback when no element index fits.
        text: Text to type (action="type").
        keys: Key chord to press, e.g. "cmd+s", "return", "escape" (action="key").
        direction: Scroll direction: up/down/left/right (action="scroll").
        amount: Scroll amount, in the driver's scroll units (action="scroll").
        from_element: Source element index for a drag.
        to_element: Destination element index for a drag.
        from_coordinate: Source pixel [x, y] for a drag.
        to_coordinate: Destination pixel [x, y] for a drag.
        modifiers: Held modifier keys, e.g. ["cmd", "shift"], for click/drag.
        raise_window: focus_app only — raise the window to the front (default False = stay in background).
        seconds: Sleep duration for action="wait".
        capture_after: Fold a follow-up screenshot into the same call after acting.

    Returns:
        The driver's JSON/text output, an app/tool listing, or a status/error
        message; for captures, the absolute path of the saved PNG.
    """
    try:
        backend = get_backend()
    except RuntimeError as exc:
        return f"[Error] {exc}"

    return await backend.dispatch(
        action=action,
        app=app,
        mode=mode,
        tool=tool,
        args=args,
        element=element,
        coordinate=coordinate,
        text=text,
        keys=keys,
        direction=direction,
        amount=amount,
        from_element=from_element,
        to_element=to_element,
        from_coordinate=from_coordinate,
        to_coordinate=to_coordinate,
        modifiers=modifiers,
        raise_window=raise_window,
        seconds=seconds,
        capture_after=capture_after,
    )
