"""Anthropic Claude Computer Use provider implementation.

Wraps the Anthropic Messages API with computer use tool and end_test
tool, then normalizes the output into LLMTurnResult.

Claude's Computer Use requires conversation history with tool_result
messages after each tool_use. This provider maintains that history
internally so the agent_runner can keep its stateless per-turn pattern.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from anthropic import Anthropic

from .base import LLMOutputItem, LLMOutputType, LLMProvider, LLMTurnResult


DEFAULT_MODEL = os.environ.get("CLAUDE_COMPUTER_MODEL", "claude-opus-4-6")

_SCROLL_STEP = 100


def _resolve_versions(model: str) -> Tuple[str, str]:
    """Pick the correct computer tool version and beta flag for the model."""
    if "opus-4-5" in model or "opus-4-6" in model:
        return "computer_20251124", "computer-use-2025-11-24"
    return "computer_20250124", "computer-use-2025-01-24"


def _normalize_action(inp: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a Claude computer-use action dict to the common format
    expected by ``map_computer_action()``."""
    action = inp.get("action", "")
    coord = inp.get("coordinate")

    if action in ("left_click", "right_click", "middle_click"):
        x, y = coord or (0, 0)
        return {"type": "click", "x": x, "y": y}

    if action == "double_click":
        x, y = coord or (0, 0)
        return {"type": "double_click", "x": x, "y": y}

    if action == "type":
        return {"type": "type", "text": inp.get("text", "")}

    if action == "key":
        return {"type": "key", "key": inp.get("text", "")}

    if action == "scroll":
        x, y = coord or (0, 0)
        direction = inp.get("scroll_direction", "down")
        amount = int(inp.get("scroll_amount", 3))
        scroll_x, scroll_y = 0, 0
        if direction == "down":
            scroll_y = amount * _SCROLL_STEP
        elif direction == "up":
            scroll_y = -(amount * _SCROLL_STEP)
        elif direction == "right":
            scroll_x = amount * _SCROLL_STEP
        elif direction == "left":
            scroll_x = -(amount * _SCROLL_STEP)
        return {"type": "scroll", "x": x, "y": y, "scroll_x": scroll_x, "scroll_y": scroll_y}

    if action == "left_click_drag":
        sx, sy = inp.get("start_coordinate") or coord or (0, 0)
        ex, ey = coord or (0, 0)
        if inp.get("start_coordinate"):
            ex, ey = coord or (ex, ey)
        return {"type": "drag", "x": sx, "y": sy, "x2": ex, "y2": ey}

    if action == "screenshot":
        return {"type": "screenshot"}

    if action == "wait":
        return {"type": "wait"}

    # mouse_move and any unknown actions are no-ops
    return {"type": "screenshot"}


def _extract_screenshot_b64(msg: Dict[str, Any]) -> Optional[str]:
    """Pull the raw base64 image data from a user message, if present."""
    content = msg.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "image":
            source = block.get("source", {})
            if source.get("type") == "base64":
                return source.get("data")
    return None


def _response_to_params(response: Any) -> List[Dict[str, Any]]:
    """Serialize response content blocks back to API-compatible params.

    Mirrors the official demo's ``_response_to_params`` — preserves thinking
    blocks with their signature so Claude can validate the chain.
    """
    params: List[Dict[str, Any]] = []
    for block in response.content:
        if block.type == "text":
            if block.text:
                params.append({"type": "text", "text": block.text})
        elif block.type == "thinking":
            entry: Dict[str, Any] = {
                "type": "thinking",
                "thinking": getattr(block, "thinking", ""),
            }
            if hasattr(block, "signature"):
                entry["signature"] = getattr(block, "signature", None)
            params.append(entry)
        elif block.type == "tool_use":
            params.append(block.model_dump())
    return params


def _extract_tool_use_blocks(response: Any) -> List[Dict[str, Any]]:
    """Return (id, name) pairs for every tool_use in the response."""
    blocks = []
    for block in response.content:
        if block.type == "tool_use":
            blocks.append({"id": block.id, "name": getattr(block, "name", "")})
    return blocks


class ClaudeProvider(LLMProvider):
    def __init__(self, model: Optional[str] = None, **kwargs: Any):
        self._client = Anthropic(**kwargs)
        self._model = model or DEFAULT_MODEL
        self._tool_version, self._beta_flag = _resolve_versions(self._model)
        self._system: Optional[str] = None
        self._history: List[Dict[str, Any]] = []
        self._pending_tool_uses: List[Dict[str, Any]] = []
        self._last_user_text: Optional[str] = None

    def format_system_message(self, text: str) -> Dict[str, Any]:
        return {"role": "system", "content": text}

    def format_user_message(self, text_parts: List[str], screenshot_data_url: Optional[str] = None) -> Dict[str, Any]:
        content: List[Dict[str, Any]] = [{"type": "text", "text": t} for t in text_parts]
        if screenshot_data_url:
            b64_data = screenshot_data_url.split(",", 1)[1]
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64_data},
            })
        return {"role": "user", "content": content}

    def _build_tools(self, display_width: int, display_height: int) -> List[Dict[str, Any]]:
        return [
            {
                "type": self._tool_version,
                "name": "computer",
                "display_width_px": display_width,
                "display_height_px": display_height,
            },
            {
                "name": "end_test",
                "description": (
                    "Signal that the test has succeeded by calling end_test "
                    "with success=true only after the goal and success criteria are satisfied."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "success": {
                            "type": "boolean",
                            "description": "Whether the test succeeded.",
                        }
                    },
                    "required": ["success"],
                },
            },
        ]

    def _make_tool_result(
        self, tool_id: str, tool_name: str, screenshot_b64: Optional[str],
    ) -> Dict[str, Any]:
        """Build a tool_result block, with screenshot only for the computer tool."""
        result: Dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "is_error": False,
        }
        if tool_name == "computer" and screenshot_b64:
            result["content"] = [{
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": screenshot_b64,
                },
            }]
        else:
            result["content"] = [{"type": "text", "text": "OK"}]
        return result

    def create_turn(
        self,
        input_messages: List[Dict[str, Any]],
        display_width: int,
        display_height: int,
    ) -> LLMTurnResult:
        system_msg = input_messages[0]
        user_msg = input_messages[1]
        system_text = system_msg["content"]

        screenshot_b64 = _extract_screenshot_b64(user_msg)

        # Extract the first text block from user message to detect substep changes
        user_text = None
        if isinstance(user_msg.get("content"), list):
            for block in user_msg["content"]:
                if isinstance(block, dict) and block.get("type") == "text":
                    user_text = block.get("text")
                    break

        # Detect context reset: new substep or first turn.
        # System prompt is constant, but the goal text in the first user
        # message changes when a new substep begins.
        first_turn = (self._system is None)
        system_changed = (system_text != self._system)
        goal_changed = (self._last_user_text is not None
                        and user_text is not None
                        and user_text != self._last_user_text)

        if first_turn or system_changed or goal_changed:
            self._system = system_text
            self._history = []
            self._pending_tool_uses = []

        if user_text:
            self._last_user_text = user_text

        if self._pending_tool_uses:
            # Send tool_result for each pending tool_use from the previous turn
            result_content: List[Dict[str, Any]] = []
            for tu in self._pending_tool_uses:
                result_content.append(
                    self._make_tool_result(tu["id"], tu["name"], screenshot_b64)
                )
            self._history.append({"role": "user", "content": result_content})
            self._pending_tool_uses = []
        else:
            # First turn: send the full user message with goal/screenshot
            self._history.append(user_msg)

        tools = self._build_tools(display_width, display_height)

        response = self._client.beta.messages.create(
            model=self._model,
            system=self._system,
            messages=self._history,
            tools=tools,
            max_tokens=4096,
            betas=[self._beta_flag],
        )

        # Serialize response content preserving thinking signatures
        response_params = _response_to_params(response)
        self._history.append({"role": "assistant", "content": response_params})

        # Track pending tool_use blocks (id + name) for next turn's tool_result
        self._pending_tool_uses = _extract_tool_use_blocks(response)

        items = self._parse_response(response)

        raw_json: Optional[str] = None
        try:
            raw_json = response.model_dump_json()
        except Exception:
            pass

        return LLMTurnResult(
            items=items,
            raw_response={"dict": response.model_dump(), "json_str": raw_json},
        )

    def _parse_response(self, response: Any) -> List[LLMOutputItem]:
        items: List[LLMOutputItem] = []
        for block in response.content:
            btype = block.type

            if btype == "thinking":
                text = getattr(block, "thinking", "")
                if text:
                    items.append(LLMOutputItem(type=LLMOutputType.REASONING, text=text))

            elif btype == "text":
                text = getattr(block, "text", "")
                if text:
                    items.append(LLMOutputItem(type=LLMOutputType.REASONING, text=text))

            elif btype == "tool_use":
                name = getattr(block, "name", "")
                inp = getattr(block, "input", {})

                if name == "computer":
                    action = _normalize_action(inp)
                    items.append(LLMOutputItem(type=LLMOutputType.COMPUTER_ACTION, action=action))
                elif name == "end_test":
                    success = bool(inp.get("success", False))
                    items.append(LLMOutputItem(type=LLMOutputType.END_TEST, success=success))

        return items
