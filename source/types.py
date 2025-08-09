"""Shared dataclasses that flow between modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class StepSpec:
    """A single substep within a test (goal + optional guidance)."""
    goal: str = ""
    suggestions: str = ""
    success_criteria: str = ""


@dataclass
class InstallConfig:
    """Controls APK installation and teardown behavior."""
    skip_install: bool = False
    uninstall_after: bool = True


@dataclass
class TestSpec:
    """Parsed representation of a JSON test specification."""
    package: str
    activity: Optional[str] = None
    apk: Optional[str] = None
    steps: List[StepSpec] = field(default_factory=list)
    pre_steps: List[Dict[str, Any]] = field(default_factory=list)
    hints: List[str] = field(default_factory=list)
    install_config: InstallConfig = field(default_factory=InstallConfig)


@dataclass
class ScreenInfo:
    """Physical and logical screen dimensions."""
    physical_width: int
    physical_height: int
    rotation_deg: int = 0

    @property
    def canvas_width(self) -> int:
        if self.rotation_deg in (90, 270):
            return self.physical_height
        return self.physical_width

    @property
    def canvas_height(self) -> int:
        if self.rotation_deg in (90, 270):
            return self.physical_width
        return self.physical_height


@dataclass
class SubstepResult:
    """Outcome of a single substep within the agent loop."""
    index: int
    goal: str
    ok: bool = False
    turns: int = 0
    suggestions: str = ""
    success_criteria: str = ""


@dataclass
class WebEvent:
    """A single event captured for the web report viewer."""
    index: int
    substep: int
    cmd: str
    x: Optional[int] = None
    y: Optional[int] = None
    image: Optional[str] = None
    physical: Optional[str] = None
    rotation: Optional[int] = None
    canvas: Optional[str] = None
    reason: str = ""
    success: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"index": self.index, "substep": self.substep, "cmd": self.cmd}
        if self.x is not None:
            d["x"] = self.x
        if self.y is not None:
            d["y"] = self.y
        if self.image is not None:
            d["image"] = self.image
        if self.physical is not None:
            d["physical"] = self.physical
        if self.rotation is not None:
            d["rotation"] = self.rotation
        if self.canvas is not None:
            d["canvas"] = self.canvas
        if self.reason:
            d["reason"] = self.reason
        if self.success is not None:
            d["success"] = self.success
        return d
