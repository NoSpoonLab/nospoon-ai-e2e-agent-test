"""
JSON-driven test runner for Android using the minimal framework in
`android_framework`. It ensures an emulator is running, installs an APK,
executes a sequence of commands, auto-captures a screenshot after each step,
draws a click marker on screenshots for tap actions, uninstalls the APK at the
end, and writes a report folder per run.

JSON format example:

{
  "apk": "apk/app-debug.apk",
  "package": "com.example.app",
  "activity": ".MainActivity",  # optional
  "steps": [
    {"cmd": "wait", "seconds": 2},
    {"cmd": "tap", "x": 540, "y": 960},
    {"cmd": "input_text", "text": "hello world"},
    {"cmd": "swipe", "x1": 100, "y1": 100, "x2": 800, "y2": 100, "duration_ms": 300},
    {"cmd": "keyevent", "code": "66"},
    {"cmd": "screenshot", "path": "reports/snap1.png"}
  ]
}

The runner prints logs and finally a JSON with the summary.
Additionally, it creates a report folder:

  reports/<YYYYMMDD_HHMMSS>_<package>/
    - summary.json
    - screenshots/step_001_<cmd>.png, step_002_<cmd>.png, ...
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime
import time

from .android_framework import AndroidDevice
from .actions import execute_command
from .app_lifecycle import parse_install_config, prepare_app, teardown_app


def load_test_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python -m source.test_runner <test.json>")
        return 2

    test_path = Path(sys.argv[1]).resolve()
    spec = load_test_json(test_path)

    package = str(spec["package"])  # required
    activity = spec.get("activity")  # optional
    steps: List[Dict[str, Any]] = list(spec.get("steps", []))

    install_config = parse_install_config(spec)

    apk_spec: Optional[str] = spec.get("apk")
    if not install_config.skip_install and not apk_spec:
        print("ERROR: 'apk' is required unless skip_install=true or skip_stall=true")
        return 2
    apk_path: Optional[Path] = Path(apk_spec).resolve() if apk_spec else None

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_root = Path("reports") / f"{ts}_{package}"
    screenshots_dir = report_root / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    events: List[Dict[str, Any]] = []

    summary: Dict[str, Any] = {
        "ok": False,
        "errors": [],
        "executed": 0,
        "apk": str(apk_path) if apk_path else "(skipped)",
        "package": package,
        "activity": activity,
        "report_dir": str(report_root),
        "steps": events,
    }
    try:
        device = AndroidDevice.connect()
        device.ensure_emulator_ready()

        prepare_app(device, package, apk_path, activity, install_config)

        for i, step in enumerate(steps, start=1):
            print(f"Executing step {i}: {step}")
            execute_command(device, step, package=package)
            # auto-screenshot after each step
            cmd_name = step.get("cmd", "step")
            auto_path = screenshots_dir / f"step_{i:03d}_{cmd_name}.png"
            try:
                if cmd_name == "tap":
                    x = int(step["x"])
                    y = int(step["y"])
                    device.screenshot_with_marker(auto_path, x, y)
                    events.append({
                        "index": i,
                        "cmd": cmd_name,
                        "x": x,
                        "y": y,
                        "image": f"screenshots/{auto_path.name}",
                    })
                else:
                    device.screenshot(auto_path)
                    evt: Dict[str, Any] = {"index": i, "cmd": cmd_name, "image": f"screenshots/{auto_path.name}"}
                    for k in ("x", "y", "x1", "y1", "x2", "y2", "duration_ms", "seconds", "text"):
                        if k in step:
                            evt[k] = step[k]
                    events.append(evt)
            except Exception as exc:
                print("Warning: auto-screenshot failed:", exc)
            summary["executed"] = i

        summary["ok"] = True
    except Exception as exc:
        msg = str(exc)
        print("ERROR:", msg)
        summary["errors"].append(msg)
        summary["ok"] = False
    # Final wait before teardown
    try:
        print("Final wait before teardown (5s)...")
        time.sleep(5)
    except Exception:
        pass

    teardown_app(device, package, install_config.uninstall_after)

    # Write summary.json in report folder
    try:
        with (report_root / "summary.json").open("w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
    except Exception as exc:
        print("Warning: failed to write report summary:", exc)

    # Generate web report (report.html)
    try:
        html_path = report_root / "report.html"
        title = f"Test Report - {package}"
        html = f"""
<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />
  <title>{title}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 16px; }}
    .info {{ margin-bottom: 12px; }}
    .viewer {{ display: flex; gap: 16px; align-items: flex-start; }}
    img {{ max-width: 60vw; height: auto; border: 1px solid #ddd; border-radius: 4px; }}
    .meta {{ min-width: 300px; }}
    .slider {{ width: 60vw; }}
    .coords {{ font-weight: bold; color: #d00; }}
    .row {{ margin: 4px 0; }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <div class=\"info\">
    <div class=\"row\"><strong>APK:</strong> {summary['apk']}</div>
    <div class=\"row\"><strong>Package:</strong> {package}</div>
    <div class=\"row\"><strong>Activity:</strong> {activity}</div>
    <div class=\"row\"><strong>Executed steps:</strong> {summary['executed']}</div>
  </div>
  <div class=\"viewer\">
    <div>
      <img id=\"shot\" src=\"\" alt=\"screenshot\" />
      <input id=\"range\" class=\"slider\" type=\"range\" min=\"1\" max=\"1\" value=\"1\" />
      <div>
        <button id=\"prev\">Prev</button>
        <button id=\"next\">Next</button>
      </div>
    </div>
    <div class=\"meta\">
      <div class=\"row\"><strong>Index:</strong> <span id=\"idx\"></span></div>
      <div class=\"row\"><strong>Command:</strong> <span id=\"cmd\"></span></div>
      <div class=\"row\"><strong>Details:</strong> <span id=\"details\"></span></div>
      <div class=\"row\"><strong>Click:</strong> <span id=\"coords\" class=\"coords\"></span></div>
    </div>
  </div>
  <script>
    const events = {json.dumps(events)};
    const img = document.getElementById('shot');
    const range = document.getElementById('range');
    const prev = document.getElementById('prev');
    const next = document.getElementById('next');
    const idxEl = document.getElementById('idx');
    const cmdEl = document.getElementById('cmd');
    const detEl = document.getElementById('details');
    const coordsEl = document.getElementById('coords');

    function show(i) {{
      const ev = events[i];
      img.src = ev.image;
      idxEl.textContent = ev.index ?? (i+1);
      cmdEl.textContent = ev.cmd ?? '';
      const det = {{...ev}}; delete det.image; delete det.cmd; delete det.index; delete det.x; delete det.y;
      detEl.textContent = JSON.stringify(det);
      coordsEl.textContent = (ev.x !== undefined && ev.y !== undefined) ? `(${{ev.x}}, ${{ev.y}})` : '-';
      range.value = i+1;
    }}

    function setMax() {{
      range.max = events.length;
    }}

    range.addEventListener('input', () => show(parseInt(range.value)-1));
    prev.addEventListener('click', () => {{ const v = Math.max(1, parseInt(range.value)-1); range.value = v; show(v-1); }});
    next.addEventListener('click', () => {{ const v = Math.min(events.length, parseInt(range.value)+1); range.value = v; show(v-1); }});

    setMax();
    if (events.length > 0) show(0);
  </script>
</body>
</html>
"""
        with html_path.open("w", encoding="utf-8") as f:
            f.write(html)
    except Exception as exc:
        print("Warning: failed to write report.html:", exc)

    print(json.dumps(summary, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
