"""
OpenAI Computer Use-driven agent runner for Android emulator via adb.

It installs and launches the target APK, then delegates UI navigation to an
LLM using the Computer Use tool. The agent requests actions (click/type/scroll
etc.) and screenshots; this module translates those into adb commands and
feeds screenshots back to the model until the goal is achieved or steps/time
exhaust.

JSON spec example:
{
  "apk": "apk/app-debug.apk",
  "package": "com.example.app",
  "activity": ".MainActivity",
  "goal": "Reach the login screen and take a screenshot"
}

Environment:
  OPENAI_API_KEY must be set.

Notes:
- This is a minimal adapter. Computer Use actions are mapped to adb:
  - mouse_move: ignored (we do not track cursor)
  - mouse_click: tap(x,y)
  - double_click: tap twice
  - scroll: swipe
  - type: input_text
  - key: keyevent
  - wait: sleep
  - screenshot: take screenshot and return base64
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import subprocess
import tempfile
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import re

# Force UTF-8 on Windows to avoid charmap codec errors from non-ASCII output
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from .android_framework import AndroidDevice
from .actions import map_computer_action, execute_command
from .app_lifecycle import parse_install_config, prepare_app, teardown_app
from .reporting import VideoRecorder, write_summary_json, write_agent_log, write_web_report
from .llm import create_provider, LLMOutputType

DEFAULT_MAX_STEPS = 250
MAX_AGENT_STEPS = int(os.environ.get("AGENT_MAX_STEPS", os.environ.get("OPENAI_AGENT_MAX_STEPS", str(DEFAULT_MAX_STEPS))))
WAIT_BETWEEN_ACTIONS = float(os.environ.get("OPENAI_AGENT_WAIT_BETWEEN_ACTIONS", "1.5"))


SYSTEM_PROMPT = (
    "You control an Android emulator screen. Use the computer tool to progress. "
    "Actions map to Android input: click => tap(x,y), drag/scroll => swipe, type => input text, "
    "key => hardware key codes (HOME=3, BACK=4, ENTER=66). After each action you will receive a fresh screenshot. "
    "You will receive context that includes 'Goal', optional 'Hints', optional 'Suggestions', "
    "optional 'Negative prompt', and optional 'Success criteria'. Treat 'Suggestions' as strict guidance. "
    "Treat 'Negative prompt' as hard constraints and never perform forbidden actions."
)


def encode_file_base64(path: Path) -> str:
    with path.open("rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def take_screenshot_b64(device: AndroidDevice, _out_dir: Path) -> str:
    """Capture a clean screenshot to a temporary file and return as data URL.

    Note: Does NOT persist the clean image under reports; it uses a temp file.
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        temp_path = Path(tf.name)
    try:
        device.screenshot(temp_path)
        data_url = "data:image/png;base64," + encode_file_base64(temp_path)
    finally:
        try:
            os.remove(temp_path)
        except Exception:
            pass
    return data_url


def take_screenshot_b64_marking(
    device: AndroidDevice,
    out_dir: Path,
    click_xy: Optional[Tuple[int, int]] = None,
    color: str = "#FF0000",
) -> str:
    """Take a screenshot; when click_xy provided, overlay a visible marker at that point.

    Returns a data URL string.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    ts_name = int(time.time() * 1000)
    tmp = out_dir / f"screen_{ts_name}.png"
    if click_xy is None:
        device.screenshot(tmp)
    else:
        x, y = click_xy
        device.screenshot_with_marker(tmp, x, y, color=color)
    return "data:image/png;base64," + encode_file_base64(tmp)


def get_device_resolution(device: AndroidDevice) -> Tuple[int, int]:
    """Return (width,height) of the device in pixels using `wm size`.

    Fallback to (1080, 2400) on failure.
    """
    try:
        cmd = ([str(device.tools.adb), "-s", device.serial] if getattr(device, "serial", None) else [str(device.tools.adb)]) + ["shell", "wm", "size"]
        cp = subprocess.run(cmd, env=device.env, capture_output=True)
        out = (cp.stdout or b"").decode("utf-8", errors="ignore")
        # Expected: Physical size: 1080x2424
        for line in out.splitlines():
            if ":" in line and "x" in line:
                part = line.split(":", 1)[1].strip()
                w_s, h_s = part.split("x")
                return int(w_s), int(h_s)
    except Exception:
        pass
    return 1080, 2400


def get_device_rotation_deg(device: AndroidDevice) -> int:
    """Return display rotation in degrees (0, 90, 180, 270).

    Uses `dumpsys input` SurfaceOrientation when available; falls back to 0.
    """
    try:
        cmd = ([str(device.tools.adb), "-s", device.serial] if getattr(device, "serial", None) else [str(device.tools.adb)]) + ["shell", "dumpsys", "input"]
        cp = subprocess.run(cmd, env=device.env, capture_output=True)
        out = (cp.stdout or b"").decode("utf-8", errors="ignore")
        for line in out.splitlines():
            if "SurfaceOrientation" in line:
                # e.g., "SurfaceOrientation: 1"
                parts = line.strip().split(":")
                if len(parts) == 2:
                    val = parts[1].strip()
                    if val.isdigit():
                        mapping = {"0": 0, "1": 90, "2": 180, "3": 270}
                        return mapping.get(val, 0)
    except Exception:
        pass
    return 0


def get_device_display_size(device: AndroidDevice) -> Optional[Tuple[int, int]]:
    """Try to read the active display size from `dumpsys display`.

    Returns (width, height) in pixels if detected, otherwise None.
    """
    try:
        cmd = ([str(device.tools.adb), "-s", device.serial] if getattr(device, "serial", None) else [str(device.tools.adb)]) + ["shell", "dumpsys", "display"]
        cp = subprocess.run(cmd, env=device.env, capture_output=True)
        out = (cp.stdout or b"").decode("utf-8", errors="ignore")
        candidates: List[Tuple[int, int]] = []
        for line in out.splitlines():
            if any(key in line for key in ("DisplayDeviceInfo", "mBaseDisplayInfo", "DisplayInfo", "deviceProductInfo")):
                m = re.search(r"(\d{3,5})\s*x\s*(\d{3,5})", line)
                if m:
                    w, h = int(m.group(1)), int(m.group(2))
                    candidates.append((w, h))
        if candidates:
            candidates.sort(key=lambda wh: wh[0] * wh[1], reverse=True)
            return candidates[0]
    except Exception:
        pass
    return None

def load_spec(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def run_agent(test_json_path: Path) -> int:
    spec = load_spec(test_json_path)
    package = str(spec["package"])  # required
    activity = spec.get("activity")
    # Optional pre-steps before agent loop
    pre_steps = list(spec.get("pre_steps", []))
    # Support multi-step agent tests: an array `steps` with per-step guidance fields.
    steps_array = spec.get("steps")
    if isinstance(steps_array, list) and steps_array and isinstance(steps_array[0], dict) and ("goal" in steps_array[0]):
        steps_spec: List[Dict[str, Any]] = steps_array  # type: ignore[assignment]
    else:
        steps_spec = [{
            "goal": str(spec.get("goal", "")).strip(),
            "suggestions": str(spec.get("suggestions", "") or "").strip(),
            "negative_prompt": str(spec.get("negative_prompt", "") or "").strip(),
            "success_criteria": str(spec.get("success_criteria", "") or "").strip(),
        }]
    # Initialize current step texts from the first step (backwards compatible if single)
    goal = str(steps_spec[0].get("goal", "")).strip()
    suggestions_text: str = str(steps_spec[0].get("suggestions", "") or "").strip()
    negative_prompt_text: str = str(steps_spec[0].get("negative_prompt", "") or "").strip()
    success_criteria_text: str = str(steps_spec[0].get("success_criteria", "") or "").strip()

    # Replace placeholders like {timestamp} in user-provided textual guidance fields.
    try:
        now_ts = str(int(time.time()))
        def _apply_placeholders(text: str) -> str:
            return text.replace("{timestamp}", now_ts)
        goal = _apply_placeholders(goal)
        if suggestions_text:
            suggestions_text = _apply_placeholders(suggestions_text)
        if negative_prompt_text:
            negative_prompt_text = _apply_placeholders(negative_prompt_text)
        if success_criteria_text:
            success_criteria_text = _apply_placeholders(success_criteria_text)
    except Exception:
        # Best-effort; if replacement fails, keep originals
        pass

    install_config = parse_install_config(spec)

    apk_spec: Optional[str] = spec.get("apk")
    if not install_config.skip_install and not apk_spec:
        print("ERROR: 'apk' is required unless skip_install=true or skip_stall=true")
        return 2
    apk: Optional[Path] = Path(apk_spec).resolve() if apk_spec else None

    # Reports base (timestamp created early to support pre-steps artifacts)
    ts = time.strftime("%Y%m%d_%H%M%S")
    report_root = Path("reports") / f"agent_{ts}_{package}"
    hints = spec.get("hints", [])

    device = AndroidDevice.connect()
    device.ensure_emulator_ready()

    try:
        prepare_app(device, package, apk, activity, install_config)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 2

    if pre_steps:
        print("Executing pre-steps before agent loop...")
        pre_dir = report_root / "pre_steps"
        pre_dir.mkdir(parents=True, exist_ok=True)
        for i, st in enumerate(pre_steps, start=1):
            print(f"Pre-step {i}: {st}")
            try:
                cmd = st.get("cmd", "")
                execute_command(device, st, package=package)
                if cmd == "tap" and "x" in st and "y" in st:
                    device.screenshot_with_marker(pre_dir / f"pre_{i:03d}_tap.png", int(st["x"]), int(st["y"]))
                else:
                    device.screenshot(pre_dir / f"pre_{i:03d}_{cmd}.png")
            except Exception as exc:
                print("Warning: pre-step failed:", exc)

    # Reports (continue with screenshots directory)
    scr_dir = report_root / "screenshots"
    scr_dir.mkdir(parents=True, exist_ok=True)
    # Directory to persist raw agent responses per step
    raw_responses_dir = report_root / "responses_raw"
    raw_responses_dir.mkdir(parents=True, exist_ok=True)
    video_path = report_root / "session.mp4"
    remote_video_path = f"/sdcard/agent_session_{ts}.mp4"
    recorder = VideoRecorder(device, remote_video_path, video_path)

    # Start device logcat capture
    device_log_path = report_root / "device_log.txt"
    logcat_proc = None
    logcat_file = None
    try:
        adb_cmd = [str(device.tools.adb)]
        if getattr(device, "serial", None):
            adb_cmd += ["-s", device.serial]
        adb_cmd += ["logcat", "-v", "threadtime"]
        logcat_file = open(device_log_path, "w", encoding="utf-8", errors="replace")
        logcat_proc = subprocess.Popen(adb_cmd, stdout=logcat_file, stderr=subprocess.STDOUT, env=device.env)
    except Exception:
        logcat_proc = None
        if logcat_file is not None:
            logcat_file.close()
            logcat_file = None

    provider = create_provider(os.environ.get("LLM_PROVIDER", "openai"))
    # Summary and accumulators
    summary: Dict[str, Any] = {
        "ok": False,
        "errors": [],
        "executed": 0,
        "report_dir": str(report_root),
        "goal": goal,
        "negative_prompt": negative_prompt_text,
        "package": package,
    }
    # Progressive logs buffer
    step_logs: List[str] = []
    # Web-report events (only marked screenshots, mainly clicks)
    web_events: List[Dict[str, Any]] = []
    # Per-substep results
    substep_results: List[Dict[str, Any]] = []

    def log(msg: str) -> None:
        try:
            print(msg)
        except UnicodeEncodeError:
            print(msg.encode("utf-8", errors="replace").decode("ascii", errors="replace"))
        step_logs.append(msg)

    try:
        recorder.start()
        log(f"[Agent] Screen recording started -> {remote_video_path}")
        # Global index to enumerate all model turns across all sub-steps
        global_turn_index = 0
        # Iterate each sub-step: all must pass
        for sub_idx, sub in enumerate(steps_spec, start=1):
            # Prepare per-substep texts (apply timestamp placeholder)
            now_ts = str(int(time.time()))
            goal_text = str(sub.get("goal", "") or "").replace("{timestamp}", now_ts).strip()
            suggestions_text = str(sub.get("suggestions", "") or "").replace("{timestamp}", now_ts).strip()
            negative_prompt_text = str(sub.get("negative_prompt", "") or "").replace("{timestamp}", now_ts).strip()
            success_criteria_text = str(sub.get("success_criteria", "") or "").replace("{timestamp}", now_ts).strip()

            # Update summary first-step goal for compatibility
            if sub_idx == 1:
                summary["goal"] = goal_text
                summary["negative_prompt"] = negative_prompt_text

            # Build persistent context for this sub-step and initial screenshot
            initial_screenshot = take_screenshot_b64(device, scr_dir)
            phy_w, phy_h = get_device_resolution(device)
            rotation = get_device_rotation_deg(device)
            if rotation in (90, 270):
                dev_w, dev_h = phy_h, phy_w
            else:
                dev_w, dev_h = phy_w, phy_h

            base_user_context = f"Goal: {goal_text}"
            if hints:
                base_user_context += "\nHints: " + " | ".join(str(h) for h in hints)
            if suggestions_text:
                base_user_context += "\nSuggestions: " + suggestions_text
            if negative_prompt_text:
                base_user_context += "\nNegative prompt (DO NOT do): " + negative_prompt_text
            if success_criteria_text:
                base_user_context += "\nSuccess criteria: " + success_criteria_text
            base_user_context += "\nInstruction: Only when the Success criteria are satisfied, call the function tool end_test with {success: true}. Otherwise continue working and do not call end_test."

            input_messages: List[Dict[str, Any]] = [
                provider.format_system_message(SYSTEM_PROMPT),
                provider.format_user_message([base_user_context], initial_screenshot),
            ]

            # Per-substep trackers
            finished = False
            explicit_success: Optional[bool] = None
            last_sig: Optional[str] = None
            repeat_count: int = 0
            turns_this_sub = 0

            while turns_this_sub < MAX_AGENT_STEPS and not finished:
                turns_this_sub += 1
                global_turn_index += 1
                log(f"[Agent] Substep {sub_idx} - Turn {turns_this_sub} (global {global_turn_index})")
                phy_w, phy_h = get_device_resolution(device)
                rotation = get_device_rotation_deg(device)
                if rotation in (90, 270):
                    dev_w, dev_h = phy_h, phy_w
                else:
                    dev_w, dev_h = phy_w, phy_h
                log(f"[Agent] Screen: physical={phy_w}x{phy_h}, rotation={rotation}°, canvas={dev_w}x{dev_h}")

                display_size = get_device_display_size(device)
                turn_result = provider.create_turn(
                    input_messages,
                    display_width=(display_size[0] if display_size else dev_w),
                    display_height=(display_size[1] if display_size else dev_h),
                )

                # Persist raw response JSON for this global turn
                try:
                    raw = turn_result.raw_response or {}
                    raw_path = raw_responses_dir / f"step_{global_turn_index:03d}_response_raw.json"
                    with raw_path.open("w", encoding="utf-8") as f:
                        json_str = raw.get("json_str") if isinstance(raw, dict) else None
                        if json_str:
                            f.write(json_str)
                        else:
                            json.dump(raw.get("dict", {}) if isinstance(raw, dict) else {}, f, indent=2)
                except Exception:
                    pass

                produced_texts: List[str] = []
                last_reasoning_text: str = ""
                executed_any = False
                actions_this_turn = 0
                last_click_xy: Optional[Tuple[int, int]] = None

                for output_item in turn_result.items:
                    if output_item.type == LLMOutputType.REASONING:
                        txt = output_item.text or ""
                        produced_texts.append(txt)
                        if txt:
                            last_reasoning_text = txt
                            log(f"[Agent] Reasoning: {txt}")

                    elif output_item.type == LLMOutputType.COMPUTER_ACTION:
                        action = output_item.action or {}
                        log(f"[Agent] Action: {action}")
                        sig = json.dumps(action, sort_keys=True)
                        if sig == last_sig:
                            repeat_count += 1
                        else:
                            repeat_count = 0
                            last_sig = sig

                        if repeat_count >= 10:
                            log("[Agent] Detected repeated action. Sending BACK to escape loop.")
                            device.keyevent("4")
                            device.wait(WAIT_BETWEEN_ACTIONS)
                            repeat_count = 0
                        else:
                            if isinstance(action, dict) and action.get("type") in ("click", "double_click") and "x" in action and "y" in action:
                                try:
                                    cx, cy = int(action.get("x")), int(action.get("y"))
                                    last_click_xy = (cx, cy)
                                    pre_marked = scr_dir / f"step_{global_turn_index:03d}_preclick_marked.png"
                                    device.screenshot_with_marker(pre_marked, cx, cy)
                                    reason_text = last_reasoning_text or (produced_texts[-1] if produced_texts else "No reasoning from model")
                                    web_events.append({
                                        "index": global_turn_index,
                                        "substep": sub_idx,
                                        "cmd": str(action.get("type")),
                                        "x": cx,
                                        "y": cy,
                                        "image": f"screenshots/{pre_marked.name}",
                                        "physical": f"{phy_w}x{phy_h}",
                                        "rotation": rotation,
                                        "canvas": f"{dev_w}x{dev_h}",
                                        "reason": reason_text,
                                    })
                                except Exception:
                                    last_click_xy = None
                            map_computer_action(device, action if isinstance(action, dict) else {})
                            actions_this_turn += 1
                            executed_any = True
                            device.wait(WAIT_BETWEEN_ACTIONS)

                    elif output_item.type == LLMOutputType.END_TEST:
                        if not output_item.success:
                            log("[Agent] Ignored end_test call with success=false; continuing without finishing.")
                            continue

                        finished = True
                        explicit_success = True
                        reason_text = last_reasoning_text or (produced_texts[-1] if produced_texts else "Model invoked end_test")
                        image_rel: Optional[str] = None
                        try:
                            end_path = scr_dir / f"step_{global_turn_index:03d}_end_test.png"
                            device.screenshot(end_path)
                            image_rel = f"screenshots/{end_path.name}"
                        except Exception:
                            image_rel = None
                        evt: Dict[str, Any] = {
                            "index": global_turn_index,
                            "substep": sub_idx,
                            "cmd": "end_test",
                            "success": True,
                            "reason": reason_text,
                        }
                        if image_rel:
                            evt["image"] = image_rel
                            evt["physical"] = f"{phy_w}x{phy_h}"
                            evt["rotation"] = rotation
                            evt["canvas"] = f"{dev_w}x{dev_h}"
                        web_events.append(evt)
                        log("[Agent] end_test tool called. success=True")
                        break

                if finished:
                    pass_flag = explicit_success if explicit_success is not None else None
                    if pass_flag is not None:
                        log(f"[Agent] Substep {sub_idx} finished. success={pass_flag}")
                else:
                    input_messages = [
                        input_messages[0],
                        provider.format_user_message(
                            [
                                base_user_context,
                                ("State updated after actions. Continue toward the goal." if executed_any else
                                 "No actions produced. Observe and continue toward the goal."),
                            ],
                            take_screenshot_b64(device, scr_dir),
                        ),
                    ]
                    log(f"[Agent] Substep {sub_idx} turn {turns_this_sub} executed actions: {actions_this_turn}")

                if finished:
                    break

            sub_ok = bool(explicit_success) if explicit_success is not None else False
            substep_results.append({
                "index": sub_idx,
                "goal": goal_text,
                "suggestions": suggestions_text,
                "negative_prompt": negative_prompt_text,
                "success_criteria": success_criteria_text,
                "ok": sub_ok,
                "turns": turns_this_sub,
            })
            if not sub_ok:
                log(f"[Agent] Aborting after substep {sub_idx} failed.")
                break

        summary["ok"] = all(s.get("ok") for s in substep_results) if substep_results else False
        summary["executed"] = global_turn_index
        summary["substeps"] = substep_results
    except Exception as exc:
        tb = traceback.format_exc()
        summary["errors"].append(str(exc))
        summary["ok"] = False
        log(f"[Agent] EXCEPTION: {exc}\n{tb}")
    finally:
        # Stop device logcat capture
        if logcat_proc is not None:
            try:
                logcat_proc.terminate()
                logcat_proc.wait(timeout=5)
            except Exception:
                try:
                    logcat_proc.kill()
                except Exception:
                    pass
        if logcat_file is not None:
            try:
                logcat_file.close()
            except Exception:
                pass
        teardown_app(device, package, install_config.uninstall_after)
        recorder.stop_and_pull()

    write_summary_json(report_root, summary)
    write_agent_log(report_root, step_logs)
    write_web_report(
        report_root, package, summary, web_events,
        substep_results, steps_spec, spec, video_path,
    )
    try:
        print(json.dumps(summary, indent=2, ensure_ascii=True))
    except UnicodeEncodeError:
        print(json.dumps(summary, indent=2, ensure_ascii=True).encode("ascii", errors="replace").decode("ascii"))
    return 0 if summary["ok"] else 1


def main() -> int:
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="AI E2E Agent Runner")
    parser.add_argument("test_json", help="Path to the test spec JSON file")
    parser.add_argument("--max-steps", type=int, default=None, help=f"Max agent turns per substep (default: {DEFAULT_MAX_STEPS})")
    args = parser.parse_args()

    if args.max_steps is not None:
        global MAX_AGENT_STEPS
        MAX_AGENT_STEPS = args.max_steps

    return run_agent(Path(args.test_json).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
