"""computer_use thin-dispatcher refactor equivalence tests.

Verify the refactored computer_use (thin dispatcher + _platforms) keeps the
public signature unchanged, selects the backend by platform, builds the right
cua-driver commands, honors REFUSALS, errors cleanly on unknown platforms, and
that _platforms is never scanned as a tool.

cua-driver is not installed, so we mock the backend run()/preflight() and only
compare the dispatched command, without actually driving a desktop.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "examples" / "haitun-workspace" / "tools"

if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def new_mod() -> Any:
    return _load(TOOLS / "computer_use.py", "cu_new_dispatcher")


@pytest.fixture
def plat() -> Any:
    return importlib.import_module("_platforms")


def _mock_backend(plat: Any) -> list[list[str]]:
    """Patch the active backend's run()/preflight(); return captured argv list."""
    backend = plat.get_backend()
    captured: list[list[str]] = []

    async def fake_run(args: list[str], *, timeout_seconds: int = 120) -> tuple[int, str]:
        captured.append(list(args))
        return 0, "OK"

    backend.run = fake_run
    backend.preflight = lambda: None
    return captured


SCENARIOS = [
    ("capture_som", {"action": "capture", "mode": "som", "app": "Finder"}),
    ("capture_vision", {"action": "capture", "mode": "vision"}),
    ("capture_ax", {"action": "capture", "mode": "ax", "app": "Safari"}),
    ("click_element", {"action": "click", "element": 7}),
    ("click_coord_mods", {"action": "click", "coordinate": [10, 20], "modifiers": ["cmd", "shift"]}),
    ("double_click", {"action": "double_click", "element": 3}),
    ("right_click", {"action": "right_click", "element": 5}),
    ("middle_click", {"action": "middle_click", "element": 2}),
    ("type", {"action": "type", "text": "hello"}),
    ("key", {"action": "key", "keys": "cmd+s"}),
    ("scroll", {"action": "scroll", "direction": "down", "amount": 5, "element": 9}),
    ("drag_elems", {"action": "drag", "from_element": 1, "to_element": 2}),
    ("drag_coords", {"action": "drag", "from_coordinate": [0, 0], "to_coordinate": [50, 50]}),
    ("focus_no_raise", {"action": "focus_app", "app": "Mail"}),
    ("focus_raise", {"action": "focus_app", "app": "Mail", "raise_window": True}),
    ("list_apps", {"action": "list_apps"}),
    ("call_passthrough", {"action": "call", "tool": "screenshot", "args": '{"mode":"vision"}'}),
    ("capture_after", {"action": "click", "element": 4, "capture_after": True}),
    ("args_override", {"action": "click", "element": 1, "args": '{"element":99,"extra":true}'}),
]


@pytest.mark.anyio
async def test_signature_unchanged(new_mod: Any) -> None:
    """Public computer_use signature stays 19 params and stays a coroutine."""
    sig = inspect.signature(new_mod.computer_use)
    assert len(sig.parameters) == 19
    assert inspect.iscoroutinefunction(new_mod.computer_use)


@pytest.mark.anyio
@pytest.mark.parametrize("label,kwargs", SCENARIOS, ids=[s[0] for s in SCENARIOS])
async def test_dispatch_builds_cua_command(new_mod: Any, plat: Any, label: str, kwargs: dict) -> None:
    """Each drive action dispatches a ``cua-driver call ...`` argv."""
    captured = _mock_backend(plat)
    await new_mod.computer_use(**kwargs)
    assert captured, f"{label}: no cua-driver call produced"
    assert captured[0][0] == "call", f"{label}: expected 'call', got {captured[0]!r}"


@pytest.mark.anyio
async def test_wait_does_not_shell_out(new_mod: Any, plat: Any) -> None:
    captured = _mock_backend(plat)
    r = await new_mod.computer_use(action="wait", seconds=0.01)
    assert r.startswith("Waited")
    assert captured == []


@pytest.mark.anyio
async def test_refusals_block_action(new_mod: Any, plat: Any) -> None:
    """A REFUSALS-listed action returns [Error] and does not shell out."""
    captured = _mock_backend(plat)
    backend = plat.get_backend()
    old = backend.REFUSALS
    backend.REFUSALS = {"drag": "not supported on this build"}
    try:
        r = await new_mod.computer_use(action="drag", from_element=1, to_element=2)
        assert r.startswith("[Error]")
        assert "not supported" in r
        assert captured == []
    finally:
        backend.REFUSALS = old


@pytest.mark.anyio
async def test_unsupported_platform_errors_cleanly(new_mod: Any, plat: Any) -> None:
    """Unknown platform returns a clean [Error], not a crash."""
    saved = plat._backend
    orig = sys.platform
    try:
        plat._backend = None
        sys.platform = "linux"  # ty: ignore[invalid-assignment]
        r = await new_mod.computer_use(action="capture")
        assert r.startswith("[Error]")
        assert "unsupported platform" in r
    finally:
        sys.platform = orig
        plat._backend = saved


def test_platforms_not_scanned_as_tool() -> None:
    """_platforms is a private package; the non-recursive *.py tool glob skips it."""
    py_files = {p.name for p in TOOLS.glob("*.py")}
    assert "computer_use.py" in py_files
    assert not any("platform" in n for n in py_files)
    assert (TOOLS / "_platforms").is_dir()
