"""Unified report generation for agent and deterministic test runs.

Encapsulates video recording, report directory setup, summary/log writing,
and web report generation.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


class VideoRecorder:
    """Manages on-device screen recording via adb screenrecord."""

    def __init__(self, device: Any, remote_path: str, local_path: Path):
        self._device = device
        self._remote_path = remote_path
        self._local_path = local_path
        self._proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        try:
            adb_cmd = [str(self._device.tools.adb)]
            if getattr(self._device, "serial", None):
                adb_cmd += ["-s", str(self._device.serial)]
            adb_cmd += ["shell", "screenrecord", "--time-limit", "7200", self._remote_path]
            self._proc = subprocess.Popen(
                adb_cmd, env=self._device.env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            self._proc = None

    def stop_and_pull(self) -> None:
        """Stop the recording process and pull the video file from device."""
        try:
            if self._proc is not None and self._proc.poll() is None:
                try:
                    adb_kill_cmd = [str(self._device.tools.adb)]
                    if getattr(self._device, "serial", None):
                        adb_kill_cmd += ["-s", str(self._device.serial)]
                    for kill_cmd in (
                        ["shell", "pkill", "-l", "screenrecord"],
                        ["shell", "killall", "-2", "screenrecord"],
                        ["shell", "sh", "-c", "kill -2 $(pidof screenrecord)"],
                    ):
                        try:
                            subprocess.run(adb_kill_cmd + kill_cmd, env=self._device.env, capture_output=True, timeout=3)
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    self._proc.wait(timeout=5)
                except Exception:
                    try:
                        self._proc.terminate()
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            adb_pull = [str(self._device.tools.adb)]
            if getattr(self._device, "serial", None):
                adb_pull += ["-s", str(self._device.serial)]
            adb_pull += ["pull", self._remote_path, str(self._local_path)]
            subprocess.run(adb_pull, env=self._device.env, capture_output=True, timeout=60)
            try:
                adb_rm = [str(self._device.tools.adb)]
                if getattr(self._device, "serial", None):
                    adb_rm += ["-s", str(self._device.serial)]
                adb_rm += ["shell", "rm", "-f", self._remote_path]
                subprocess.run(adb_rm, env=self._device.env, capture_output=True, timeout=10)
            except Exception:
                pass
        except Exception:
            pass

    @property
    def local_path(self) -> Path:
        return self._local_path


def init_report_dirs(package: str, timestamp: str, prefix: str = "agent") -> Path:
    """Create and return the report root directory with standard subdirectories."""
    report_root = Path("reports") / f"{prefix}_{timestamp}_{package}"
    (report_root / "screenshots").mkdir(parents=True, exist_ok=True)
    (report_root / "responses_raw").mkdir(parents=True, exist_ok=True)
    return report_root


def write_summary_json(report_root: Path, summary: Dict[str, Any]) -> None:
    report_root.mkdir(parents=True, exist_ok=True)
    summary['result'] = 'passed' if summary.get('ok') else 'failed'
    with (report_root / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)


def write_agent_log(report_root: Path, step_logs: List[str]) -> None:
    try:
        with (report_root / "agent_log.txt").open("w", encoding="utf-8") as f:
            for line in step_logs:
                f.write(line + "\n")
    except Exception:
        pass


def write_web_report(
    report_root: Path,
    package: str,
    summary: Dict[str, Any],
    web_events: List[Dict[str, Any]],
    substep_results: List[Dict[str, Any]],
    steps_spec: List[Dict[str, Any]],
    spec: Dict[str, Any],
    video_path: Optional[Path] = None,
) -> None:
    """Generate the web report (report_data.json, report_data.js, templates)."""
    try:
        is_multi = isinstance(spec.get("steps"), list) and len(spec.get("steps")) > 1
        first_step = steps_spec[0] if steps_spec else {}
        report_goal = (f"Multi-step test: {len(steps_spec)} steps" if is_multi else str(first_step.get("goal", "")))
        report_suggestions = ("" if is_multi else str(first_step.get("suggestions", "")))
        report_negative_prompt = ("" if is_multi else str(first_step.get("negative_prompt", "")))
        report_success = ("" if is_multi else str(first_step.get("success_criteria", "")))

        has_video = video_path is not None and video_path.exists() and video_path.stat().st_size > 0

        data = {
            "meta": {
                "title": f"Agent Report - {package}",
                "goal": report_goal,
                "executed": summary.get("executed", 0),
                "package": package,
                "suggestions": report_suggestions,
                "negative_prompt": report_negative_prompt,
                "success_criteria": report_success,
                "result": summary.get("result", "failed"),
                "ok": bool(summary.get("ok", False)),
                "video": ("session.mp4" if has_video else ""),
            },
            "events": web_events,
            "substeps": substep_results,
        }
        with (report_root / "report_data.json").open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        with (report_root / "report_data.js").open("w", encoding="utf-8") as f:
            f.write("window.REPORT_DATA=")
            json.dump(data, f, separators=(",", ":"))
            f.write(";")

        templates_dir = Path(__file__).parent / "templates"
        html_tpl = templates_dir / "agent_report.html"
        css_tpl = templates_dir / "agent_report.css"
        if html_tpl.exists():
            shutil.copyfile(str(html_tpl), str(report_root / "report.html"))
        else:
            with (report_root / "report.html").open("w", encoding="utf-8") as f:
                f.write("<!DOCTYPE html><html><head><meta charset='utf-8'><title>Agent Report</title></head><body><pre>Template missing. See report_data.json</pre></body></html>")
        if css_tpl.exists():
            shutil.copyfile(str(css_tpl), str(report_root / "report.css"))
    except Exception as exc:
        print("Warning: failed to write templated report:", exc)
