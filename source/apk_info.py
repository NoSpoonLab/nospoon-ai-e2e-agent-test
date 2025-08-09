"""
Extract Android APK metadata (package name and launchable activity) using
`aapt` from the Unity-bundled SDK.

Usage:
  python -m source.apk_info <path-to-apk>

Prints a JSON object with fields: package, launchable_activity.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .emulator_setup import locate_android_tools


def find_aapt_path(sdk_root: Path) -> Optional[Path]:
    for exe in ("aapt.exe", "aapt2.exe", "aapt", "aapt2"):
        for p in sdk_root.rglob(exe):
            if p.is_file():
                return p
    return None


def dump_badging(aapt_path: Path, apk_path: Path) -> str:
    cp = subprocess.run([str(aapt_path), "dump", "badging", str(apk_path)], capture_output=True)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.decode("utf-8", errors="ignore") or "aapt dump badging failed")
    return cp.stdout.decode("utf-8", errors="ignore")


def parse_package_and_activity(badging: str) -> tuple[Optional[str], Optional[str]]:
    pkg = None
    act = None
    m = re.search(r"package: name='([^']+)'", badging)
    if m:
        pkg = m.group(1)
    m2 = re.search(r"launchable-activity: name='([^']+)'", badging)
    if m2:
        act = m2.group(1)
    return pkg, act


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python -m source.apk_info <apk>")
        return 2
    apk_path = Path(sys.argv[1]).resolve()
    tools = locate_android_tools()
    aapt = find_aapt_path(tools.sdk_root)
    if not aapt:
        print(json.dumps({"ok": False, "error": "aapt not found under SDK"}))
        return 1
    try:
        badging = dump_badging(aapt, apk_path)
        pkg, act = parse_package_and_activity(badging)
        print(json.dumps({"ok": True, "package": pkg, "launchable_activity": act}, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())


