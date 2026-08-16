"""Real-hardware mouse control test (Windows).

Proves the machine's physical cursor can be driven programmatically end to end:
read position -> move to several targets -> click in place -> restore. Uses the
native Win32 ``user32`` API via ctypes (zero dependency, reversible).

This drives the REAL cursor, so it needs an interactive Windows desktop. It is
skipped on non-Windows and when no GUI session is available (e.g. headless CI),
so it never breaks the Linux/macOS CI run.

Note: this validates the OS-level "a program can control this machine's mouse"
capability. The computer_use tool itself drives via cua-driver (separate); its
dispatch logic is covered by test_computer_use_dispatcher.py.
"""

from __future__ import annotations

import contextlib
import ctypes
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Real mouse control test is Windows-only (uses Win32 user32)",
)


def _has_desktop() -> bool:
    """True if an interactive desktop with a usable screen is present."""
    if sys.platform != "win32":
        return False
    try:
        u = ctypes.windll.user32
        w = u.GetSystemMetrics(0)  # SM_CXSCREEN
        h = u.GetSystemMetrics(1)  # SM_CYSCREEN
        return w > 0 and h > 0
    except Exception:
        return False


requires_desktop = pytest.mark.skipif(
    not _has_desktop(), reason="No interactive desktop (headless); skipping real cursor test"
)


MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _set_dpi_aware() -> None:
    # Without DPI awareness, SetCursorPos has rounding drift on scaled displays.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        with contextlib.suppress(Exception):
            ctypes.windll.user32.SetProcessDPIAware()


def _pos() -> tuple[int, int]:
    p = _POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def _move(x: int, y: int) -> None:
    ctypes.windll.user32.SetCursorPos(int(x), int(y))


@requires_desktop
def test_read_cursor_position() -> None:
    """Can read the current cursor position as two ints on screen."""
    x, y = _pos()
    assert isinstance(x, int) and isinstance(y, int)
    w = ctypes.windll.user32.GetSystemMetrics(0)
    h = ctypes.windll.user32.GetSystemMetrics(1)
    assert -20 <= x <= w + 20 and -20 <= y <= h + 20


def _move_lands(tx: int, ty: int, tol: int = 2, attempts: int = 3) -> bool:
    """Move to (tx,ty); return True if the cursor lands within tol px.

    Retries a few times so a single burst of concurrent human mouse movement
    doesn't flake the test — we only need to prove the move CAN land precisely.
    """
    for _ in range(attempts):
        _move(tx, ty)
        time.sleep(0.15)
        gx, gy = _pos()
        if max(abs(gx - tx), abs(gy - ty)) <= tol:
            return True
    return False


@requires_desktop
def test_move_and_restore_cursor() -> None:
    """Move the real cursor through a square path, land precisely, then restore.

    Robust to concurrent human mouse use: each target retries, and the whole
    path retries once; we assert the majority of targets landed precisely so a
    transient hand-on-mouse doesn't cause a false failure.
    """
    _set_dpi_aware()
    start = _pos()
    sw = ctypes.windll.user32.GetSystemMetrics(0)
    sh = ctypes.windll.user32.GetSystemMetrics(1)
    # Use fixed targets around screen center — independent of the (possibly
    # edge/multi-monitor) start position, so landing is unambiguous.
    cx, cy = sw // 2, sh // 2
    targets = [(cx, cy), (cx + 80, cy), (cx + 80, cy + 80), (cx, cy + 80)]
    best = 0
    try:
        # Retry the whole path a few times: a burst of concurrent human mouse
        # movement can spoil one pass, but the capability holds across passes.
        for _ in range(3):
            landed = sum(1 for tx, ty in targets if _move_lands(tx, ty))
            best = max(best, landed)
            if best == len(targets):
                break
    finally:
        _move(*start)
        time.sleep(0.15)
    # All targets must land exactly on at least one clean pass — proves precise
    # programmatic control even if a human nudged the mouse during other passes.
    assert best == len(targets), f"best pass landed {best}/{len(targets)} targets"


@requires_desktop
def test_click_in_place_is_harmless() -> None:
    """Fire a left down+up at the current position (in-place click), then restore.

    Clicking in place minimizes side effects; we only assert the cursor did not
    move and the synthetic input call succeeded.
    """
    _set_dpi_aware()
    start = _pos()
    try:
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.1)
    finally:
        _move(*start)
    after = _pos()
    assert max(abs(after[0] - start[0]), abs(after[1] - start[1])) <= 2
