"""Shared app installation, launch, and teardown logic.

Consolidates the duplicated install/launch/uninstall sequences from
agent_runner and test_runner into reusable functions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .android_framework import AndroidDevice
from .types import InstallConfig


def parse_install_config(spec: Dict[str, Any]) -> InstallConfig:
    """Parse skip_install / uninstall_after from a test spec dict."""
    skip_install = bool(spec.get("skip_install", False) or spec.get("skip_stall", False))
    uninstall_after = bool(spec.get("uninstall_after", not skip_install))
    if bool(spec.get("skip_stall", False)):
        uninstall_after = False
    return InstallConfig(skip_install=skip_install, uninstall_after=uninstall_after)


def prepare_app(
    device: AndroidDevice,
    package: str,
    apk: Optional[Path],
    activity: Optional[str],
    config: InstallConfig,
) -> None:
    """Install (if needed), force-stop, and launch the target app.

    Raises RuntimeError for config errors (missing APK, package not installed).
    """
    if config.skip_install:
        print("Skipping APK installation (skip_install/skip_stall=true). Assuming it is already on device.")
        if not device.is_package_installed(package):
            raise RuntimeError(f"Package not installed on device: {package} (skip_install/skip_stall=true)")
    else:
        if device.is_package_installed(package):
            print("Package already installed, uninstalling:", package)
            device.uninstall(package)
        print("Installing APK:", apk)
        device.install_apk(apk)  # type: ignore[arg-type]

    try:
        print("Force-stopping app before launch:", package)
        device.stop_app(package)
    except Exception:
        pass
    if activity or package:
        device.launch_app(package, activity)


def teardown_app(device: AndroidDevice, package: str, uninstall: bool) -> None:
    """Optionally uninstall the app at the end of a test run."""
    if uninstall:
        try:
            print("Uninstalling APK:", package)
            device.uninstall(package)
        except Exception:
            pass
    else:
        print("Keeping app installed (uninstall_after=false).")
