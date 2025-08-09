"""
Minimal Android automation framework on top of adb, tailored for Unity-bundled
SDK on Windows. It exposes a small set of commands inspired by Airtest
(`touch`, `swipe`, `input_text`, etc.) and utilities to install/launch an app.

This module relies on `source.emulator_setup` to locate SDK/JDK and to ensure an
emulator is available.

All logs are printed in English. Comments are in English per repository rules.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from .emulator_setup import (
    AndroidTools,
    locate_android_tools,
    build_env,
    list_avds,
    start_emulator,
    wait_for_boot,
    list_adb_devices,
    kill_emulator,
    wait_for_emulator_shutdown,
)

try:
    from PIL import Image, ImageDraw
except Exception:
    Image = None
    ImageDraw = None


def _run(cmd: List[str], env: Dict[str, str], check: bool = True, capture: bool = True, input_text: Optional[bytes] = None) -> subprocess.CompletedProcess:
    printable = " ".join([shlex.quote(str(c)) for c in cmd])
    print(f"$ {printable}")
    cp = subprocess.run(
        cmd,
        env=env,
        input=input_text,
        capture_output=capture,
    )
    if capture:
        if cp.stdout:
            try:
                print(cp.stdout.decode("utf-8", errors="ignore"))
            except Exception:
                pass
        if cp.stderr:
            try:
                print(cp.stderr.decode("utf-8", errors="ignore"))
            except Exception:
                pass
    if check and cp.returncode != 0:
        raise RuntimeError(f"Command failed (exit {cp.returncode}): {printable}")
    return cp


def _adb(env: Dict[str, str], tools: AndroidTools, args: List[str], check: bool = True, capture: bool = True, input_text: Optional[bytes] = None, serial: Optional[str] = None) -> subprocess.CompletedProcess:
    base = [str(tools.adb)]
    if serial:
        base += ["-s", serial]
    return _run(base + args, env=env, check=check, capture=capture, input_text=input_text)


def _sanitize_text_for_adb_input(text: str) -> str:
    """Sanitize text for `adb shell input text`.

    Spaces must be replaced by %s. Some characters must be escaped or
    are unsupported; replace with spaces as a conservative fallback.
    """

    # Replace spaces
    text = text.replace(" ", "%s")
    # Replace characters that break shell parsing or input
    # Keep alphanumerics and a small safe set.
    safe = re.compile(r"[^A-Za-z0-9_%@.,:\-]")
    text = safe.sub("_", text)
    return text


@dataclass
class AndroidDevice:
    tools: AndroidTools
    env: Dict[str, str]
    serial: Optional[str] = None

    @classmethod
    def connect(cls) -> "AndroidDevice":
        tools = locate_android_tools()
        env = build_env(tools)
        device = cls(tools=tools, env=env, serial=None)
        device.serial = device._select_preferred_serial()
        return device

    # ---------- AVD helpers ----------

    def _query_avd_name(self, serial: str) -> Optional[str]:
        """Return the AVD name for a running emulator serial, or None."""
        try:
            cp = _adb(self.env, self.tools, ["emu", "avd", "name"], check=False, serial=serial)
            out = (cp.stdout or b"").decode("utf-8", errors="ignore").strip()
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            return lines[-1] if lines else None
        except Exception:
            return None

    def _select_preferred_serial(self, prefer_name: Optional[str] = None) -> Optional[str]:
        """Pick the best running emulator serial.

        Prefers an emulator whose AVD name matches prefer_name (or "ai_device"
        by default). Falls back to the first online emulator.
        """
        devices = list_adb_devices(self.tools, self.env)
        emulator_serials = [s for (s, st) in devices if s.startswith("emulator-") and st == "device"]
        preferred_names = {"ai device", "ai_device"}
        if prefer_name:
            preferred_names.add(prefer_name.lower().replace(" ", "_"))
        for s in emulator_serials:
            name = self._query_avd_name(s)
            if name and name.lower().replace(" ", "_") in preferred_names:
                return s
        return emulator_serials[0] if emulator_serials else None

    # ---------- Device readiness ----------

    def ensure_emulator_ready(self) -> None:
        """Start an emulator if none is running and wait until boot completes."""

        selected = self._select_preferred_serial()
        if selected:
            print("An emulator is already running.")
            self.serial = selected
            return

        avds = list_avds(self.tools, self.env)
        if not avds:
            raise RuntimeError("No AVDs available. Run emulator_setup.py first to create one.")

        target = None
        for name in avds:
            norm = name.lower().replace(" ", "_")
            if norm in ("ai_device",):
                target = name
                break
        if target is None:
            target = avds[0]
        print(f"Starting emulator: {target}")
        start_emulator(self.tools, self.env, target)
        print("Waiting for emulator boot...")
        wait_for_boot(self.tools, self.env)
        print("Emulator is ready.")
        selected = self._select_preferred_serial(prefer_name=target)
        if selected is None:
            try:
                cp = _run([str(self.tools.adb), "-e", "get-serialno"], env=self.env, check=False)
                ser = (cp.stdout or b"").decode("utf-8", errors="ignore").strip()
                if ser:
                    selected = ser
            except Exception:
                pass
        self.serial = self.serial or selected

    # ---------- App lifecycle ----------

    def install_apk(self, apk_path: Path, replace: bool = True, allow_test: bool = True) -> None:
        args = ["install"]
        if replace:
            args.append("-r")
        if allow_test:
            args.append("-t")
        args.append(str(apk_path))
        # First try without raising to inspect the error output
        cp = _adb(self.env, self.tools, args, check=False, serial=self.serial)
        if cp.returncode == 0:
            return
        stderr = (cp.stderr or b"").decode("utf-8", errors="ignore")
        stdout = (cp.stdout or b"").decode("utf-8", errors="ignore")
        combined = f"{stdout}\n{stderr}".lower()
        space_indicators = (
            "install_failed_insufficient_storage",
            "not enough space",
            "requested internal only",
        )
        should_recover = any(tok in combined for tok in space_indicators)
        if should_recover:
            print("Install failed due to low space. Restarting emulator with wipe-data and larger partition, then retrying...")
            # Default to 2047 MB (emulator max) if not specified
            part_env = os.environ.get("EMULATOR_PARTITION_SIZE_MB")
            try:
                partition_size_mb = int(part_env) if part_env else 2047
            except Exception:
                partition_size_mb = 2047
            # Clamp to emulator limits 10..2047
            if partition_size_mb < 10:
                partition_size_mb = 10
            if partition_size_mb > 2047:
                partition_size_mb = 2047
            self.restart_emulator(wipe_data=True, partition_size_mb=partition_size_mb)
            # Retry once
            cp2 = _adb(self.env, self.tools, args, check=False, serial=self.serial)
            if cp2.returncode == 0:
                return
            stderr2 = (cp2.stderr or b"").decode("utf-8", errors="ignore")
            raise RuntimeError(f"Failed to install APK after recovery: {stderr2 or 'unknown error'}")
        raise RuntimeError(f"Failed to install APK: exit {cp.returncode}")

    def restart_emulator(self, wipe_data: bool = False, partition_size_mb: Optional[int] = None) -> None:
        """Restart the emulator, optionally wiping data and resizing the partition.

        Selects the same target AVD used by ensure_emulator_ready when possible.
        """
        # Try to kill current emulator
        try:
            kill_emulator(self.tools, self.env, self.serial)
            wait_for_emulator_shutdown(self.tools, self.env, self.serial)
        except Exception:
            pass

        avds = list_avds(self.tools, self.env)
        if not avds:
            raise RuntimeError("No AVDs available to restart.")
        target = None
        for name in avds:
            norm = name.lower().replace(" ", "_")
            if norm in ("ai_device",):
                target = name
                break
        if target is None:
            target = avds[0]
        print(f"Starting emulator: {target} (wipe_data={wipe_data}, partition_size_mb={partition_size_mb})")
        start_emulator(self.tools, self.env, target, wipe_data=wipe_data, partition_size_mb=partition_size_mb)
        wait_for_boot(self.tools, self.env)
        print("Emulator is ready.")
        selected = self._select_preferred_serial(prefer_name=target)
        self.serial = selected or self.serial

    def uninstall(self, package: str, keep_data: bool = False) -> None:
        args = ["uninstall"]
        if keep_data:
            args.append("-k")
        args.append(package)
        _adb(self.env, self.tools, args, check=False, serial=self.serial)

    def is_package_installed(self, package: str) -> bool:
        """Return True if the package is present on the device."""
        try:
            cp = _adb(self.env, self.tools, ["shell", "pm", "list", "packages", package], check=False, serial=self.serial)
            out = (cp.stdout or b"").decode("utf-8", errors="ignore")
            # Lines look like: package:com.example.app
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("package:") and line.endswith(package):
                    return True
        except Exception:
            pass
        return False

    def launch_app(self, package: str, activity: Optional[str] = None) -> None:
        if activity:
            comp = activity if "/" in activity else f"{package}/{activity}"
            _adb(self.env, self.tools, ["shell", "am", "start", "-n", comp], serial=self.serial)
        else:
            # Fallback: monkey to trigger launcher activity
            _adb(self.env, self.tools, ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"], serial=self.serial)

    def stop_app(self, package: str) -> None:
        _adb(self.env, self.tools, ["shell", "am", "force-stop", package], serial=self.serial)

    # ---------- Input interactions ----------

    def tap(self, x: int, y: int) -> None:
        _adb(self.env, self.tools, ["shell", "input", "tap", str(x), str(y)], serial=self.serial)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        _adb(self.env, self.tools, [
            "shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)
        ], serial=self.serial)

    def input_text(self, text: str) -> None:
        sanitized = _sanitize_text_for_adb_input(text)
        _adb(self.env, self.tools, ["shell", "input", "text", sanitized], serial=self.serial)

    def keyevent(self, name_or_code: str) -> None:
        _adb(self.env, self.tools, ["shell", "input", "keyevent", str(name_or_code)], serial=self.serial)

    def back(self) -> None:
        self.keyevent("4")

    def home(self) -> None:
        self.keyevent("3")

    # ---------- Waits / queries ----------

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)

    def wait_activity(self, package: str, activity: Optional[str] = None, timeout_sec: int = 30) -> None:
        """Poll the resumed activity until it matches the package/activity."""

        end = time.time() + timeout_sec
        expected = None
        if activity:
            expected = activity if "/" in activity else f"{package}/{activity}"
        while time.time() < end:
            cp = _adb(self.env, self.tools, ["shell", "dumpsys", "activity", "activities"], check=False)
            out = (cp.stdout or b"").decode("utf-8", errors="ignore")
            # Look for a line like: ResumedActivity: ... package/.Activity
            m = re.search(r"ResumedActivity:.*? (\S+)/(\S+)", out)
            if m:
                comp = f"{m.group(1)}/{m.group(2)}"
                if expected is None and comp.startswith(package + "/"):
                    return
                if expected is not None and comp == expected:
                    return
            time.sleep(0.5)
        raise TimeoutError("Timed out waiting for target activity to be resumed")

    # ---------- Artifacts ----------

    def screenshot(self, out_path: Path) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cp = subprocess.run(
            ([str(self.tools.adb), "-s", self.serial] if self.serial else [str(self.tools.adb)]) + ["exec-out", "screencap", "-p"],
            env=self.env,
            capture_output=True,
            timeout=15,
        )
        if cp.returncode != 0:
            raise RuntimeError("Failed to take screenshot")
        with out_path.open("wb") as f:
            f.write(cp.stdout)

    def screenshot_with_marker(self, out_path: Path, x: int, y: int, color: str = "#FF0000") -> None:
        """Take a screenshot and overlay a highly visible marker at (x, y)."""
        try:
            self.screenshot(out_path)
        except Exception:
            # If capture fails (device busy/disconnecting), skip silently
            return
        if Image is None:
            return
        try:
            base = Image.open(out_path).convert("RGBA")
            w, h = base.size
            overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)

            # Dynamic size based on screen dimensions
            r = max(40, int(min(w, h) * 0.05))
            outline_w = max(6, int(r * 0.18))
            shadow_w = outline_w + 4

            # Shadow (white) for contrast
            bbox = (x - r, y - r, x + r, y + r)
            draw.ellipse(bbox, outline="#FFFFFF", width=shadow_w)
            draw.line((x - r, y, x + r, y), fill="#FFFFFF", width=shadow_w)
            draw.line((x, y - r, x, y + r), fill="#FFFFFF", width=shadow_w)

            # Semi-transparent fill + red outline
            fill_rgba = (255, 0, 0, 64)
            draw.ellipse(bbox, fill=fill_rgba, outline=color, width=outline_w)
            draw.line((x - r, y, x + r, y), fill=color, width=outline_w)
            draw.line((x, y - r, x, y + r), fill=color, width=outline_w)

            # Composite overlay
            result = Image.alpha_composite(base, overlay).convert("RGB")
            result.save(out_path)
        except Exception:
            pass


