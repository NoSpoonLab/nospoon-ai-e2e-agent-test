"""
Unity Android environment validation script for Windows.

This script scans `C:\\Program Files\\Unity\\Hub\\Editor` for the highest available
Unity version and verifies that the required Android components under
`AndroidPlayer` exist:

- SDK (adb, emulator, avdmanager, sdkmanager)
- NDK (ndk-build)
- OpenJDK (java, javac)

It prints a human-readable log and, at the end, a JSON summary to stdout.
It does not write report files. Exits 0 when everything is OK, or 1 if any
critical component is missing.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .emulator_setup import find_unity_versions, pick_unity_version, find_first_existing, search_file_recursively


def build_report(
    program_files: Path,
    unity_base: Path,
    selected_version: Optional[str],
    android_player_path: Optional[Path],
    sdk_path: Optional[Path],
    ndk_path: Optional[Path],
    jdk_path: Optional[Path],
) -> Dict:
    """Build the full report dictionary."""

    report: Dict = {
        "program_files": str(program_files),
        "unity": {
            "base_path": str(unity_base),
            "selected_version": selected_version,
            "android_player_path": str(android_player_path) if android_player_path else None,
            "exists": bool(android_player_path and android_player_path.exists()),
        },
        "android_components": {
            "sdk": {
                "path": str(sdk_path) if sdk_path else None,
                "exists": bool(sdk_path and sdk_path.exists()),
                "tools": {},
            },
            "ndk": {
                "path": str(ndk_path) if ndk_path else None,
                "exists": bool(ndk_path and ndk_path.exists()),
                "tools": {},
            },
            "openjdk": {
                "path": str(jdk_path) if jdk_path else None,
                "exists": bool(jdk_path and jdk_path.exists()),
                "tools": {},
            },
        },
        "ok": False,
        "errors": [],
    }

    # SDK tools
    if sdk_path and sdk_path.exists():
        adb = find_first_existing([
            sdk_path / "platform-tools" / "adb.exe",
            sdk_path / "platform-tools" / "adb",
        ])
        emulator = find_first_existing([
            sdk_path / "emulator" / "emulator.exe",
            sdk_path / "emulator" / "emulator",
        ])
        avdmanager = find_first_existing([
            sdk_path / "cmdline-tools" / "latest" / "bin" / "avdmanager.bat",
            sdk_path / "cmdline-tools" / "latest" / "bin" / "avdmanager",
            sdk_path / "cmdline-tools" / "bin" / "avdmanager.bat",
            sdk_path / "cmdline-tools" / "bin" / "avdmanager",
            sdk_path / "tools" / "bin" / "avdmanager.bat",
            sdk_path / "tools" / "bin" / "avdmanager",
        ])
        sdkmanager = find_first_existing([
            sdk_path / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat",
            sdk_path / "cmdline-tools" / "latest" / "bin" / "sdkmanager",
            sdk_path / "cmdline-tools" / "bin" / "sdkmanager.bat",
            sdk_path / "cmdline-tools" / "bin" / "sdkmanager",
            sdk_path / "tools" / "bin" / "sdkmanager.bat",
            sdk_path / "tools" / "bin" / "sdkmanager",
        ])

        report["android_components"]["sdk"]["tools"] = {
            "adb": str(adb) if adb else None,
            "emulator": str(emulator) if emulator else None,
            "avdmanager": str(avdmanager) if avdmanager else None,
            "sdkmanager": str(sdkmanager) if sdkmanager else None,
        }

    # NDK tools
    if ndk_path and ndk_path.exists():
        ndk_build = search_file_recursively(ndk_path, ["ndk-build.cmd", "ndk-build"])
        report["android_components"]["ndk"]["tools"] = {
            "ndk_build": str(ndk_build) if ndk_build else None,
        }

    # JDK tools
    if jdk_path and jdk_path.exists():
        java = find_first_existing([
            jdk_path / "bin" / "java.exe",
            jdk_path / "bin" / "java",
        ])
        javac = find_first_existing([
            jdk_path / "bin" / "javac.exe",
            jdk_path / "bin" / "javac",
        ])
        report["android_components"]["openjdk"]["tools"] = {
            "java": str(java) if java else None,
            "javac": str(javac) if javac else None,
        }

    # Compute errors and global OK flag
    errors: List[str] = []
    if not report["unity"]["exists"]:
        errors.append("AndroidPlayer folder not found")
    if not report["android_components"]["sdk"]["exists"]:
        errors.append("SDK folder not found")
    else:
        tools = report["android_components"]["sdk"]["tools"]
        if not tools.get("adb"):
            errors.append("adb not found in SDK")
        if not tools.get("emulator"):
            errors.append("emulator not found in SDK")
        if not tools.get("avdmanager"):
            errors.append("avdmanager not found in SDK")
        if not tools.get("sdkmanager"):
            errors.append("sdkmanager not found in SDK")

    if not report["android_components"]["ndk"]["exists"]:
        errors.append("NDK folder not found")
    else:
        if not report["android_components"]["ndk"]["tools"].get("ndk_build"):
            errors.append("ndk-build not found in NDK")

    if not report["android_components"]["openjdk"]["exists"]:
        errors.append("OpenJDK folder not found")
    else:
        jdk_tools = report["android_components"]["openjdk"]["tools"]
        if not jdk_tools.get("java"):
            errors.append("java not found in OpenJDK")
        if not jdk_tools.get("javac"):
            errors.append("javac not found in OpenJDK")

    report["errors"] = errors
    report["ok"] = len(errors) == 0

    return report


def main(argv: Optional[List[str]] = None) -> int:
    """Script entrypoint."""
    program_files = Path(os.environ.get("ProgramFiles", r"C:\\Program Files")).resolve()
    unity_hub_editor = program_files / "Unity" / "Hub" / "Editor"

    available_versions = find_unity_versions(unity_hub_editor)
    selected_version = pick_unity_version(available_versions, prefer=None)

    android_player_path: Optional[Path] = None
    editor_root: Optional[Path] = None
    if selected_version is not None:
        editor_root = unity_hub_editor / selected_version / "Editor"
        candidate_android = editor_root / "Data" / "PlaybackEngines" / "AndroidPlayer"
        if candidate_android.exists():
            android_player_path = candidate_android

    sdk_path = android_player_path / "SDK" if android_player_path else None
    ndk_path = android_player_path / "NDK" if android_player_path else None
    jdk_path = android_player_path / "OpenJDK" if android_player_path else None

    report = build_report(
        program_files=program_files,
        unity_base=unity_hub_editor,
        selected_version=selected_version,
        android_player_path=android_player_path,
        sdk_path=sdk_path,
        ndk_path=ndk_path,
        jdk_path=jdk_path,
    )

    # Human-readable log
    print("Unity Hub Editor base:", report["unity"]["base_path"])
    print("Selected Unity version:", report["unity"]["selected_version"])
    print("AndroidPlayer path:", report["unity"]["android_player_path"])
    print("SDK path:", report["android_components"]["sdk"]["path"])
    print("NDK path:", report["android_components"]["ndk"]["path"])
    print("OpenJDK path:", report["android_components"]["openjdk"]["path"])

    if report["ok"]:
        print("Result: OK - Unity Android environment is ready.")
    else:
        print("Result: MISSING - Issues found:")
        for err in report["errors"]:
            print(" -", err)
    print(json.dumps(report, indent=2))

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
