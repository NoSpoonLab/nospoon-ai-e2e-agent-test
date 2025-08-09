"""Unified command dispatch for both deterministic and agent-driven tests.

Consolidates run_step() from test_runner and execute_pre_step/map_computer_action
from agent_runner into a single module.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional

from .android_framework import AndroidDevice


def execute_command(device: AndroidDevice, step: Dict[str, Any], package: str = "") -> None:
    """Execute a deterministic test command (tap, swipe, wait, etc.).

    Used by test_runner steps, agent_runner pre_steps, and anywhere a JSON
    command dict needs to be turned into an ADB action.
    """
    cmd = step.get("cmd")
    if not cmd:
        return

    if cmd == "wait":
        device.wait(float(step.get("seconds", 1)))
    elif cmd == "tap":
        device.tap(int(step["x"]), int(step["y"]))
    elif cmd == "swipe":
        device.swipe(
            int(step["x1"]), int(step["y1"]),
            int(step["x2"]), int(step["y2"]),
            int(step.get("duration_ms", 300)),
        )
    elif cmd == "input_text":
        device.input_text(str(step["text"]))
    elif cmd == "keyevent":
        device.keyevent(str(step.get("code") or step.get("name")))
    elif cmd == "back":
        device.back()
    elif cmd == "home":
        device.home()
    elif cmd == "screenshot":
        device.screenshot(Path(step["path"]))
    elif cmd == "launch":
        device.launch_app(str(step.get("package", package)), step.get("activity"))
    elif cmd == "stop":
        device.stop_app(str(step.get("package", package)))
    else:
        raise ValueError(f"Unknown command: {cmd}")


def map_computer_action(device: AndroidDevice, action: Dict[str, Any]) -> Optional[str]:
    """Execute an LLM Computer Use action via ADB. Returns optional status string."""

    atype = action.get("type")
    x = action.get("x")
    y = action.get("y")
    if atype == "click":
        if x is None or y is None:
            return "error: missing coordinates"
        device.tap(int(x), int(y))
        return "success"
    if atype == "double_click":
        if x is None or y is None:
            return "error: missing coordinates"
        device.tap(int(x), int(y))
        time.sleep(0.1)
        device.tap(int(x), int(y))
        return "success"
    if atype == "drag":
        x2 = action.get("x2")
        y2 = action.get("y2")
        duration_ms = int(action.get("duration_ms", 300))
        if x2 is None or y2 is None:
            path = action.get("path")
            if isinstance(path, list) and len(path) >= 2:
                try:
                    start = path[0]
                    end = path[-1]
                    x = start.get("x", x)
                    y = start.get("y", y)
                    x2 = end.get("x")
                    y2 = end.get("y")
                except Exception:
                    pass
        if None in (x, y, x2, y2):
            return "error: missing drag coordinates"
        device.swipe(int(x), int(y), int(x2), int(y2), duration_ms)
        return "success"
    if atype == "scroll":
        dx = action.get("dx")
        dy = action.get("dy")
        if dx is None:
            dx = action.get("scroll_x")
        if dy is None:
            dy = action.get("scroll_y")
        duration_ms = int(action.get("duration_ms", 300))
        if None in (x, y, dx, dy):
            return "error: missing scroll parameters"
        device.swipe(int(x), int(y), int(int(x) + int(dx)), int(int(y) + int(dy)), duration_ms)
        return "success"
    if atype == "type":
        text = action.get("text", "")
        device.input_text(str(text))
        return "success"
    if atype == "key":
        key = str(action.get("key") or action.get("code") or "")
        if not key:
            return "error: missing key"
        device.keyevent(key)
        return "success"
    if atype == "wait":
        seconds = float(action.get("seconds", 1))
        device.wait(seconds)
        return "success"
    if atype == "screenshot":
        return "success"
    return None
