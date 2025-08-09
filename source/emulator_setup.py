"""
Ensure an Android emulator exists and boot it using the SDK bundled with
Unity on Windows.

Behavior:
- Automatically detects the highest Unity version in `C:\\Program Files\\Unity\\Hub\\Editor`.
- Locates `AndroidPlayer` and required tools: sdkmanager, avdmanager, emulator, adb, and OpenJDK.
- If no AVD with the target name exists, installs a suitable system image and creates it.
- Boots the emulator and waits until the system completes boot.

Output:
- Human-readable logs during execution
- Final JSON summary
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------- Defaults ----------------------------

DEFAULT_PROGRAM_FILES = Path(os.environ.get("ProgramFiles", r"C:\\Program Files")).resolve()
DEFAULT_AVD_NAME = "UnityTestAVD"

# These images are attempted in order until one can be installed/created.
SYSTEM_IMAGE_CANDIDATES = [
    "system-images;android-34;google_apis;x86_64",
    "system-images;android-33;google_apis;x86_64",
]


# ---------------------------- Environment helpers ----------------------------


def find_unity_versions(base_unity_hub: Path) -> List[str]:
    if not base_unity_hub.exists() or not base_unity_hub.is_dir():
        return []
    versions = [p.name for p in base_unity_hub.iterdir() if p.is_dir()]
    versions.sort(reverse=True)
    return versions


def pick_unity_version(available: List[str], prefer: Optional[str] = None) -> Optional[str]:
    if not available:
        return None
    if prefer and prefer in available:
        return prefer
    return available[0]


def find_first_existing(paths: List[Path]) -> Optional[Path]:
    for p in paths:
        if p and p.exists():
            return p
    return None


def search_file_recursively(root: Path, candidate_names: List[str]) -> Optional[Path]:
    if not root.exists():
        return None
    for name in candidate_names:
        for found in root.rglob(name):
            if found.is_file():
                return found
    return None


@dataclass
class AndroidTools:
    sdk_root: Path
    jdk_root: Path
    adb: Path
    emulator: Path
    avdmanager: Path
    sdkmanager: Path


def locate_android_tools(program_files: Path = DEFAULT_PROGRAM_FILES) -> AndroidTools:
    """Locate Android SDK/JDK and required binaries.

    Resolution order:
    1) Respect environment variables on CI or developer machines:
       - ANDROID_SDK_ROOT or ANDROID_HOME
       - JAVA_HOME
    2) Fallback to Unity-bundled AndroidPlayer on Windows (historical default).

    Raises RuntimeError if critical components are missing.
    """

    # ---------- 1) CI / generic environment via env vars ----------
    env_sdk = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
    if env_sdk:
        sdk_root = Path(env_sdk).resolve()
        if not sdk_root.exists():
            raise RuntimeError(f"ANDROID_SDK_ROOT not found: {sdk_root}")

        # Prefer JAVA_HOME if provided; some tools may not need it but we wire it when available
        env_jdk = os.environ.get("JAVA_HOME")
        jdk_root = Path(env_jdk).resolve() if env_jdk else Path("")

        adb = find_first_existing([
            sdk_root / "platform-tools" / "adb",
            sdk_root / "platform-tools" / "adb.exe",
        ])
        emulator = find_first_existing([
            sdk_root / "emulator" / "emulator",
            sdk_root / "emulator" / "emulator.exe",
            sdk_root / "tools" / "emulator",
            sdk_root / "tools" / "emulator.exe",
        ])
        avdmanager = find_first_existing([
            sdk_root / "cmdline-tools" / "latest" / "bin" / "avdmanager",
            sdk_root / "cmdline-tools" / "latest" / "bin" / "avdmanager.bat",
            sdk_root / "cmdline-tools" / "bin" / "avdmanager",
            sdk_root / "cmdline-tools" / "bin" / "avdmanager.bat",
            sdk_root / "tools" / "bin" / "avdmanager",
            sdk_root / "tools" / "bin" / "avdmanager.bat",
        ])
        sdkmanager = find_first_existing([
            sdk_root / "cmdline-tools" / "latest" / "bin" / "sdkmanager",
            sdk_root / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat",
            sdk_root / "cmdline-tools" / "bin" / "sdkmanager",
            sdk_root / "cmdline-tools" / "bin" / "sdkmanager.bat",
            sdk_root / "tools" / "bin" / "sdkmanager",
            sdk_root / "tools" / "bin" / "sdkmanager.bat",
        ])

        if not (adb and emulator):
            raise RuntimeError("Required Android tools not found in ANDROID_SDK_ROOT (adb/emulator).")

        # avdmanager/sdkmanager may be absent on minimal images; keep None detection friendly to callers that don't need them
        if not avdmanager:
            # Try to find anywhere under SDK just in case
            found = search_file_recursively(sdk_root, ["avdmanager", "avdmanager.bat"]) or None
            avdmanager = found or (sdk_root / "tools" / "bin" / "avdmanager")
        if not sdkmanager:
            found = search_file_recursively(sdk_root, ["sdkmanager", "sdkmanager.bat"]) or None
            sdkmanager = found or (sdk_root / "tools" / "bin" / "sdkmanager")

        # If JAVA_HOME was not provided, jdk_root may be empty; downstream will avoid relying on it if empty
        if not jdk_root or not str(jdk_root):
            jdk_root = Path(os.environ.get("JAVA_HOME", "")) or Path("")

        return AndroidTools(
            sdk_root=sdk_root,
            jdk_root=jdk_root if str(jdk_root) else sdk_root,  # fallback to sdk_root to keep PATH composition safe
            adb=adb,
            emulator=emulator,
            avdmanager=avdmanager if avdmanager else (sdk_root / "tools" / "bin" / "avdmanager"),
            sdkmanager=sdkmanager if sdkmanager else (sdk_root / "tools" / "bin" / "sdkmanager"),
        )

    # ---------- 2) Windows Unity fallback ----------
    unity_hub_editor = program_files / "Unity" / "Hub" / "Editor"
    versions = find_unity_versions(unity_hub_editor)
    selected = pick_unity_version(versions, prefer="6000.0.62f1")
    if not selected:
        raise RuntimeError("No Unity versions found under Program Files.")

    android_player = unity_hub_editor / selected / "Editor" / "Data" / "PlaybackEngines" / "AndroidPlayer"
    if not android_player.exists():
        raise RuntimeError("AndroidPlayer folder not found under Unity installation.")

    sdk_root = android_player / "SDK"
    ndk_root = android_player / "NDK"
    jdk_root = android_player / "OpenJDK"

    if not sdk_root.exists():
        raise RuntimeError("SDK folder not found.")
    if not jdk_root.exists():
        raise RuntimeError("OpenJDK folder not found.")

    adb = find_first_existing([
        sdk_root / "platform-tools" / "adb.exe",
        sdk_root / "platform-tools" / "adb",
    ])
    emulator = find_first_existing([
        sdk_root / "emulator" / "emulator.exe",
        sdk_root / "emulator" / "emulator",
    ])
    avdmanager = find_first_existing([
        sdk_root / "cmdline-tools" / "latest" / "bin" / "avdmanager.bat",
        sdk_root / "cmdline-tools" / "latest" / "bin" / "avdmanager",
        sdk_root / "cmdline-tools" / "bin" / "avdmanager.bat",
        sdk_root / "cmdline-tools" / "bin" / "avdmanager",
        sdk_root / "tools" / "bin" / "avdmanager.bat",
        sdk_root / "tools" / "bin" / "avdmanager",
    ])
    sdkmanager = find_first_existing([
        sdk_root / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat",
        sdk_root / "cmdline-tools" / "latest" / "bin" / "sdkmanager",
        sdk_root / "cmdline-tools" / "bin" / "sdkmanager.bat",
        sdk_root / "cmdline-tools" / "bin" / "sdkmanager",
        sdk_root / "tools" / "bin" / "sdkmanager.bat",
        sdk_root / "tools" / "bin" / "sdkmanager",
    ])

    if not (adb and emulator and avdmanager and sdkmanager):
        raise RuntimeError("Required Android tools not found (adb/emulator/avdmanager/sdkmanager).")

    return AndroidTools(
        sdk_root=sdk_root,
        jdk_root=jdk_root,
        adb=adb,
        emulator=emulator,
        avdmanager=avdmanager,
        sdkmanager=sdkmanager,
    )


# ---------------------------- Subprocess helpers ----------------------------


def build_env(tools: AndroidTools) -> Dict[str, str]:
    """Build environment variables for SDK tool invocations."""

    env = os.environ.copy()
    env["JAVA_HOME"] = str(tools.jdk_root)
    env["ANDROID_HOME"] = str(tools.sdk_root)
    env["ANDROID_SDK_ROOT"] = str(tools.sdk_root)
    env_path = env.get("PATH", "")
    extra = [
        str(tools.sdk_root / "platform-tools"),
        str(tools.sdk_root / "emulator"),
        str(tools.sdk_root / "tools" / "bin"),
        str(tools.sdk_root / "cmdline-tools" / "latest" / "bin"),
        str(tools.jdk_root / "bin"),
    ]
    env["PATH"] = os.pathsep.join(extra + [env_path])
    return env


def run(cmd: List[str], env: Optional[Dict[str, str]] = None, input_text: Optional[str] = None, check: bool = True, capture: bool = True, cwd: Optional[Path] = None, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    """Run a command, print logs, and return the CompletedProcess."""

    printable = " ".join([shlex.quote(str(c)) for c in cmd])
    print(f"$ {printable}")
    result = subprocess.run(
        cmd,
        input=(input_text.encode("utf-8") if input_text is not None else None),
        env=env,
        cwd=str(cwd) if cwd else None,
        capture_output=capture,
        timeout=timeout,
    )
    if capture:
        if result.stdout:
            try:
                print(result.stdout.decode("utf-8", errors="ignore"))
            except Exception:
                pass
        if result.stderr:
            try:
                print(result.stderr.decode("utf-8", errors="ignore"))
            except Exception:
                pass
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed (exit {result.returncode}): {printable}")
    return result


# ---------------------------- AVD / emulator logic ----------------------------


def list_avds(tools: AndroidTools, env: Dict[str, str]) -> List[str]:
    cp = run([str(tools.emulator), "-list-avds"], env=env, check=False)
    out = (cp.stdout or b"").decode("utf-8", errors="ignore")
    return [line.strip() for line in out.splitlines() if line.strip()]


def ensure_system_image(tools: AndroidTools, env: Dict[str, str]) -> str:
    """Install (if needed) a system image and return its path-id.

    Tries several candidates until installation succeeds. Automatically accepts licenses.
    """

    # Accept licenses first
    try:
        run([str(tools.sdkmanager), "--licenses"], env=env, input_text=("y\n" * 100), check=False)
    except Exception:
        # Non-critical if it fails; installs will prompt again
        pass

    last_error: Optional[str] = None
    for image_id in SYSTEM_IMAGE_CANDIDATES:
        try:
            print(f"Attempting to install system image: {image_id}")
            run([str(tools.sdkmanager), "--install", image_id], env=env, input_text=("y\n" * 100), check=True)
            # Ensure platforms and emulator are present
            platform = image_id.split(";")[1]
            run([str(tools.sdkmanager), "--install", f"platforms;{platform}"], env=env, input_text=("y\n" * 100), check=False)
            run([str(tools.sdkmanager), "--install", "emulator"], env=env, input_text=("y\n" * 100), check=False)
            return image_id
        except Exception as exc:
            last_error = str(exc)
            print(f"Install failed for {image_id}: {last_error}")
            continue
    raise RuntimeError(f"No system image could be installed. Last error: {last_error}")


def create_avd_if_missing(tools: AndroidTools, env: Dict[str, str], avd_name: str) -> str:
    existing = list_avds(tools, env)
    if avd_name in existing:
        print(f"AVD already exists: {avd_name}")
        return avd_name

    image_id = ensure_system_image(tools, env)
    print(f"Creating AVD '{avd_name}' with image '{image_id}'")
    # avdmanager may ask for confirmation; answer newline to skip skin setup
    run([
        str(tools.avdmanager), "create", "avd",
        "-n", avd_name,
        "-k", image_id,
        "--force",
    ], env=env, input_text="\n", check=True)
    return avd_name


def start_emulator(
    tools: AndroidTools,
    env: Dict[str, str],
    avd_name: str,
    wipe_data: bool = False,
    partition_size_mb: Optional[int] = None,
) -> None:
    # Start the emulator non-blocking; use flags that make headless startup faster.
    args = [
        str(tools.emulator),
        "-avd", avd_name,
        "-no-snapshot",
        "-no-boot-anim",
        "-netdelay", "none",
        "-netspeed", "full",
    ]

    # Allow environment overrides when caller does not specify explicitly
    if partition_size_mb is None:
        try:
            env_part = os.environ.get("EMULATOR_PARTITION_SIZE_MB")
            if env_part:
                partition_size_mb = int(env_part)
        except Exception:
            partition_size_mb = None
    if not wipe_data:
        wipe_env = os.environ.get("EMULATOR_WIPE_DATA", "").strip().lower()
        wipe_data = wipe_env in ("1", "true", "yes")

    if wipe_data:
        args.append("-wipe-data")
    if partition_size_mb and partition_size_mb > 0:
        # The emulator expects integer MB value and enforces 10..2047
        val = int(partition_size_mb)
        if val < 10:
            print("Note: partition_size_mb too small; clamping to 10MB")
            val = 10
        if val > 2047:
            print("Note: partition_size_mb too large; clamping to 2047MB due to emulator limits")
            val = 2047
        args += ["-partition-size", str(val)]

    # Keep emulator alive by not capturing output; spawn with Popen.
    printable = " ".join([shlex.quote(str(c)) for c in args])
    print(f"$ {printable} &")
    subprocess.Popen(args, env=env)


def wait_for_boot(tools: AndroidTools, env: Dict[str, str], timeout_sec: int = 600) -> None:
    """Wait for device to appear and for `sys.boot_completed` to be 1."""

    start = time.time()
    # Wait until adb detects the device
    while True:
        try:
            cp = run([str(tools.adb), "wait-for-device"], env=env, check=False, capture=False, timeout=30)
            break
        except Exception:
            if time.time() - start > timeout_sec:
                raise TimeoutError("Timeout waiting for device to appear via adb")
            time.sleep(2)

    # Wait for boot property
    while True:
        try:
            cp = run([str(tools.adb), "shell", "getprop", "sys.boot_completed"], env=env, check=False)
            val = (cp.stdout or b"").decode("utf-8", errors="ignore").strip()
            if val == "1":
                # A veces el launcher tarda un poco más
                time.sleep(5)
                return
        except Exception:
            pass
        if time.time() - start > timeout_sec:
            raise TimeoutError("Timeout waiting for Android system to complete boot")
        time.sleep(3)


def list_adb_devices(tools: AndroidTools, env: Dict[str, str]) -> List[Tuple[str, str]]:
    """List adb devices as (serial, state)."""
    cp = run([str(tools.adb), "devices"], env=env, check=False)
    out = (cp.stdout or b"").decode("utf-8", errors="ignore").splitlines()
    devices: List[Tuple[str, str]] = []
    for line in out[1:]:  # skip header
        line = line.strip()
        if not line:
            continue
        if line.startswith("*"):
            # daemon output lines
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1]))
    return devices


def pick_emulator_serial(devices: List[Tuple[str, str]]) -> Optional[str]:
    """Return the first online emulator serial (emulator-XXXX)."""
    for serial, state in devices:
        if serial.startswith("emulator-") and state == "device":
            return serial
    # if none is 'device', return any that starts with emulator-
    for serial, _ in devices:
        if serial.startswith("emulator-"):
            return serial
    return None


def kill_emulator(tools: AndroidTools, env: Dict[str, str], serial: Optional[str]) -> None:
    """Try to stop the emulator via `adb emu kill`."""
    if serial:
        run([str(tools.adb), "-s", serial, "emu", "kill"], env=env, check=False)
    else:
        # -e: first emulator
        run([str(tools.adb), "-e", "emu", "kill"], env=env, check=False)


def wait_for_emulator_shutdown(tools: AndroidTools, env: Dict[str, str], prev_serial: Optional[str], timeout_sec: int = 60) -> None:
    """Wait until there are no online emulators or the given serial disappears."""
    start = time.time()
    while True:
        devices = list_adb_devices(tools, env)
        if prev_serial:
            if not any(s == prev_serial for s, _ in devices):
                return
        else:
            if not any(s.startswith("emulator-") for s, _ in devices):
                return
        if time.time() - start > timeout_sec:
            print("Warning: emulator did not shutdown within timeout.")
            return
        time.sleep(2)


def main() -> int:
    summary: Dict[str, object] = {
        "ok": False,
        "errors": [],
        "actions": [],
        "avd_name": None,
    }
    try:
        tools = locate_android_tools()
        env = build_env(tools)

        print("Detected SDK:", tools.sdk_root)
        print("Detected JDK:", tools.jdk_root)
        print("Tools:")
        print(" - adb:", tools.adb)
        print(" - emulator:", tools.emulator)
        print(" - avdmanager:", tools.avdmanager)
        print(" - sdkmanager:", tools.sdkmanager)

        existing = list_avds(tools, env)
        print("Existing AVDs:", ", ".join(existing) if existing else "<none>")

        # Si existe al menos un AVD, usar el primero existente para evitar instalaciones
        if existing:
            target_avd = existing[0]
            print(f"Using existing AVD: {target_avd}")
        else:
            # No hay AVDs, creamos uno nuevo (esto puede requerir instalar imágenes)
            target_avd = create_avd_if_missing(tools, env, DEFAULT_AVD_NAME)
            summary["actions"].append(f"created:{target_avd}")

        summary["avd_name"] = target_avd

        start_emulator(tools, env, target_avd)
        summary["actions"].append(f"started:{target_avd}")

        print("Waiting for emulator to boot...")
        wait_for_boot(tools, env)
        print("Emulator is ready.")

        # Cerrar emulador al finalizar con éxito
        print("Shutting down emulator...")
        devices_before_kill = list_adb_devices(tools, env)
        serial = pick_emulator_serial(devices_before_kill)
        kill_emulator(tools, env, serial)
        wait_for_emulator_shutdown(tools, env, serial)
        summary["actions"].append(f"stopped:{target_avd}")

        summary["ok"] = True
    except Exception as exc:
        msg = str(exc)
        print("ERROR:", msg)
        summary["errors"].append(msg)
        summary["ok"] = False

    # JSON final
    print(json.dumps(summary, indent=2))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())


