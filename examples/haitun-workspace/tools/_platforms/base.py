"""Backend base class — shared cua-driver runtime for every platform.

This holds the machinery that does not change between macOS and Windows:

- locating / preflighting the ``cua-driver`` CLI,
- running it (:func:`anyio.run_process`) with a timeout,
- merging the raw ``args`` JSON override into a call payload,
- writing screenshots under ``generated/computer_use/``,
- the diagnostic subcommands (``doctor`` / ``permissions`` / ``list-tools`` /
  ``describe`` / ``version`` / ``setup``),
- the shared ``dispatch`` state machine that turns a friendly ``action`` into a
  ``cua-driver call <tool> '<json>'`` invocation.

Per-platform subclasses (:class:`~_platforms.mac.MacBackend`,
:class:`~_platforms.win.WinBackend`) only declare their install hint, their
permission wording, and their capability ledger (``REFUSALS``) — the actual
action surface presented to the model is identical everywhere.

Nothing here is a tool: the file is private (``_platforms``) and is never scanned
by the tool registry (non-recursive ``*.py`` glob that also skips ``_`` names).
"""

from __future__ import annotations

import json
import os
import shutil
import time
from typing import ClassVar

import anyio

# cua-driver binary name. The installer adds it to the User PATH, but a
# long-running host process (e.g. the gateway started before install, or from a
# shell whose PATH wasn't refreshed) won't see that update. So we resolve the
# real executable: PATH first, then the known per-OS install locations. This is
# why computer_use could wrongly report "cua-driver CLI not found" even when it
# was installed — the host's PATH simply hadn't picked up the installer's entry.
_BIN_NAME = "cua-driver"


def _resolve_bin() -> str:
    """Locate the cua-driver executable: PATH, then known install dirs."""
    found = shutil.which(_BIN_NAME)
    if found:
        return found
    home = os.path.expanduser("~")
    localappdata = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
    candidates = [
        # Windows installer shim + packaged current release.
        os.path.join(localappdata, "Programs", "Cua", "cua-driver", "bin", "cua-driver.exe"),
        os.path.join(home, ".cua-driver", "packages", "current", "cua-driver.exe"),
        # macOS / Linux.
        os.path.join(home, ".local", "bin", "cua-driver"),
        "/usr/local/bin/cua-driver",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return _BIN_NAME  # fall back to bare name (will fail preflight with a clear hint)


# Resolved once at import; still just the bare name if nothing is found.
_BIN = _resolve_bin()

# Official install guide (per-OS one-line installer). Subclasses build the exact
# command; this doc URL is shared.
INSTALL_DOC = "https://cua.ai/docs/how-to-guides/driver/install"

# Where captured PNGs are written (relative to the workspace cwd, git-ignored).
_SHOT_DIR = os.path.join("generated", "computer_use")

# Real cua-driver subcommands (not MCP tools invoked through ``call``).
_SUBCOMMANDS = {
    "setup": None,
    "doctor": ["doctor"],
    "permissions": ["permissions", "status"],
    "list_tools": ["list-tools"],
    "version": ["--version"],
}


class Backend:
    """Shared cua-driver backend. Subclass per OS; override the class attrs below.

    Subclasses set:
      - ``os_name``   — human name used in hints ("macOS" / "Windows").
      - ``REFUSALS``  — ``{action: reason}``; a non-empty entry makes ``dispatch``
        refuse that action on this platform (macOS keeps this empty).
    """

    os_name: str = ""
    #: Actions this platform declines, mapped to a user-facing reason. Empty on
    #: platforms with the full action surface (macOS).
    REFUSALS: ClassVar[dict[str, str]] = {}

    # ------------------------------------------------------------------ hints
    def install_hint(self) -> str:
        """Return an OS-appropriate cua-driver install hint. Override per OS."""
        return (
            f"Install cua-driver (guide: {INSTALL_DOC}):\n"
            f"  See the official install guide: {INSTALL_DOC}\n"
            "  cua-driver doctor              # verify the install"
        )

    # -------------------------------------------------------------- preflight
    def preflight(self) -> str | None:
        """Return an error string if cua-driver can't be used here, else None."""
        # _BIN is an absolute path when resolved from a known install dir, or the
        # bare name if only found on PATH / not found at all.
        if os.path.isfile(_BIN) or shutil.which(_BIN):
            return None
        return f"[Error] `{_BIN_NAME}` CLI not found.\n{self.install_hint()}"

    # -------------------------------------------------------------------- run
    async def run(self, args: list[str], *, timeout_seconds: int = 120) -> tuple[int, str]:
        """Run ``cua-driver <args>`` and return (returncode, combined out+err)."""
        try:
            with anyio.fail_after(timeout_seconds):
                result = await anyio.run_process([_BIN, *args], check=False)
        except TimeoutError:
            return 124, f"[Error] {_BIN} timed out after {timeout_seconds}s."
        out = result.stdout.decode("utf-8", errors="replace")
        err = result.stderr.decode("utf-8", errors="replace")
        return result.returncode, (out + err).strip()

    # ------------------------------------------------------------ arg merging
    @staticmethod
    def merge_args(base: dict[str, object], raw: str) -> dict[str, object]:
        """Merge a raw JSON overrides string into *base* (raw wins on clashes)."""
        if not raw.strip():
            return base
        try:
            extra = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"'args' is not valid JSON: {exc}") from exc
        if not isinstance(extra, dict):
            raise ValueError("'args' must be a JSON object, e.g. '{\"pid\": 512}'.")
        return {**base, **extra}

    # -------------------------------------------------------------- dispatch
    async def dispatch(
        self,
        *,
        action: str,
        app: str,
        mode: str,
        tool: str,
        args: str,
        element: int | None,
        coordinate: list[int] | None,
        text: str,
        keys: str,
        direction: str,
        amount: int,
        from_element: int | None,
        to_element: int | None,
        from_coordinate: list[int] | None,
        to_coordinate: list[int] | None,
        modifiers: list[str] | None,
        raise_window: bool,
        seconds: float,
        capture_after: bool,
    ) -> str:
        """Turn a friendly ``action`` into a cua-driver invocation and return output.

        This is the shared state machine; ``computer_use.py`` forwards its
        arguments here verbatim. Platform differences live in ``install_hint``
        and ``REFUSALS`` only.
        """
        if err := self.preflight():
            return err
        action = action.strip().lower()

        # Capability ledger: refuse actions this platform does not support.
        if reason := self.REFUSALS.get(action):
            return f"[Error] action={action!r} is not supported on {self.os_name}: {reason}"

        # --- Diagnostic subcommands (not MCP tools) --------------------------
        if action == "setup":
            _code, text_out = await self.run(["doctor"])
            status = text_out or "(no output)"
            return f"{self.install_hint()}\n\n--- cua-driver doctor ---\n{status}"
        if action == "describe":
            if not tool.strip():
                return "[Error] describe requires 'tool' (the MCP tool name to inspect)."
            _code, text_out = await self.run(["describe", tool.strip()])
            return text_out or f"[Error] Could not describe tool {tool!r}."
        if action in _SUBCOMMANDS:
            argv = _SUBCOMMANDS[action]
            # ``setup`` (the only None value) is handled above, so argv is a list here.
            assert argv is not None
            _code, text_out = await self.run(argv)
            return text_out or f"[Error] `{_BIN} {' '.join(argv)}` produced no output."

        if action == "wait":
            await anyio.sleep(max(0.0, seconds))
            return f"Waited {max(0.0, seconds)}s."

        # --- Drive / MCP-tool actions (via `cua-driver call <tool> '<json>'`) -
        # Map the friendly action to a cua-driver MCP tool name (overridable by `tool`).
        # cua-driver 0.20 renamed capture tools and dropped the ``mode`` param:
        #   capture mode=ax          -> get_accessibility_tree (AX/UIA text, no image)
        #   capture mode=vision|som  -> get_desktop_state       (full-display PNG)
        # These tools use ``additionalProperties: false``, so we must NOT send
        # ``mode``/``app`` to them. An explicit ``tool=`` still overrides the mapping.
        capture_mode = (mode.strip() or "som") if action == "capture" else ""
        if tool.strip():
            tool_name = tool.strip()
        elif action == "capture":
            tool_name = "get_accessibility_tree" if capture_mode == "ax" else "get_desktop_state"
        else:
            tool_name = action

        # Tools whose schema rejects unknown fields — send only what they accept.
        _capture_tools = {"get_desktop_state", "get_accessibility_tree"}

        # Browser (CDP) tools drive web-page elements, not desktop windows. Their
        # schemas differ from desktop input tools and reject desktop-only fields
        # (element/keys/direction/from_*/modifiers/app). We only pass the few web
        # concepts (coordinate for a viewport point, text for typing); everything
        # else (pid, ref, url, …) travels through ``args`` verbatim. cua-driver
        # 0.20 browser tools: browser_navigate / browser_click / browser_type /
        # browser_pointer / browser_prepare / get_browser_state.
        is_browser = tool_name.startswith("browser_") or tool_name == "get_browser_state"

        payload: dict[str, object] = {}
        if is_browser:
            if coordinate:
                payload["coordinate"] = coordinate
            if text:
                payload["text"] = text
        else:
            if app.strip() and tool_name not in _capture_tools:
                payload["app"] = app.strip()
            if element is not None:
                payload["element"] = element
            if coordinate:
                payload["coordinate"] = coordinate
            if text:
                payload["text"] = text
            if keys.strip():
                payload["keys"] = keys.strip()
            if direction.strip():
                payload["direction"] = direction.strip()
            if amount:
                payload["amount"] = amount
            if from_element is not None:
                payload["from_element"] = from_element
            if to_element is not None:
                payload["to_element"] = to_element
            if from_coordinate:
                payload["from_coordinate"] = from_coordinate
            if to_coordinate:
                payload["to_coordinate"] = to_coordinate
            if modifiers:
                payload["modifiers"] = modifiers
            if action == "focus_app":
                payload["raise_window"] = raise_window
            if capture_after and action != "capture":
                payload["capture_after"] = True

        try:
            payload = self.merge_args(payload, args)
        except ValueError as exc:
            return f"[Error] {exc}"

        call_args = ["call", tool_name, json.dumps(payload)]

        # A screenshot comes back as an image content block; extract it to a file.
        # capture (unless ax-only) and any action with capture_after produce one.
        wants_image = not is_browser and (capture_after or (action == "capture" and (mode.strip() or "som") != "ax"))
        shot_path = ""
        if wants_image:
            shot_dir = anyio.Path(_SHOT_DIR)
            await shot_dir.mkdir(parents=True, exist_ok=True)
            shot_path = str(await (shot_dir / f"shot-{int(time.time() * 1000)}.png").resolve())
            call_args += ["--screenshot-out-file", shot_path]

        code, out = await self.run(call_args)

        if code != 0:
            detail = out or "(no output)"
            return (
                f"[Error] `{_BIN} call {tool_name}` failed (exit {code}): {detail}\n"
                f"Hint: run action='list_tools' / action='describe' tool='{tool_name}' to check the schema."
            )

        if shot_path and await anyio.Path(shot_path).exists():
            note = out.strip()
            suffix = f"\n{note}" if note else ""
            return f"Screenshot saved: {shot_path}\nDeliver it to the user with MEDIA:{shot_path}{suffix}"
        return out or f"{tool_name} ok."
