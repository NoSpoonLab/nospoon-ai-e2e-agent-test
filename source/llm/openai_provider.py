"""OpenAI Computer Use provider implementation.

Wraps the OpenAI Responses API with computer_use_preview tool and end_test
function tool, then normalizes the output into LLMTurnResult.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from openai import OpenAI

from .base import LLMOutputItem, LLMOutputType, LLMProvider, LLMTurnResult


DEFAULT_MODEL = os.environ.get("OPENAI_COMPUTER_MODEL", "computer-use-preview")


class OpenAIProvider(LLMProvider):
    def __init__(self, model: Optional[str] = None, **kwargs: Any):
        self._client = OpenAI(**kwargs)
        self._model = model or DEFAULT_MODEL

    def format_system_message(self, text: str) -> Dict[str, Any]:
        return {
            "role": "system",
            "content": [{"type": "input_text", "text": text}],
        }

    def format_user_message(self, text_parts: List[str], screenshot_data_url: Optional[str] = None) -> Dict[str, Any]:
        content: List[Dict[str, Any]] = [{"type": "input_text", "text": t} for t in text_parts]
        if screenshot_data_url:
            content.append({"type": "input_image", "image_url": screenshot_data_url})
        return {"role": "user", "content": content}

    def create_turn(
        self,
        input_messages: List[Dict[str, Any]],
        display_width: int,
        display_height: int,
    ) -> LLMTurnResult:
        resp = self._client.responses.create(
            model=self._model,
            input=input_messages,
            tools=[
                {
                    "type": "computer_use_preview",
                    "display_width": display_width,
                    "display_height": display_height,
                    "environment": "browser",
                },
                {
                    "type": "function",
                    "name": "end_test",
                    "description": (
                        "Signal that the test has succeeded by calling end_test "
                        "with success=true only after the goal and success criteria are satisfied."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "success": {
                                "type": "boolean",
                                "description": "Whether the test succeeded.",
                            }
                        },
                        "required": ["success"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                },
            ],
            reasoning={"summary": "concise"},
            truncation="auto",
        )

        resp_dict = resp.model_dump()
        items = self._parse_outputs(resp_dict.get("output", []))

        raw_json: Optional[str] = None
        try:
            raw_json = resp.model_dump_json()
        except Exception:
            pass

        return LLMTurnResult(items=items, raw_response={"dict": resp_dict, "json_str": raw_json})

    def _parse_outputs(self, outputs: List[Dict[str, Any]]) -> List[LLMOutputItem]:
        items: List[LLMOutputItem] = []
        for item in outputs:
            itype = item.get("type")
            if itype == "reasoning":
                for part in (item.get("summary") or []):
                    if part.get("type") == "summary_text":
                        txt = part.get("text", "")
                        if txt:
                            items.append(LLMOutputItem(type=LLMOutputType.REASONING, text=txt))

            elif itype == "computer_call":
                action = item.get("action") or {}
                items.append(LLMOutputItem(type=LLMOutputType.COMPUTER_ACTION, action=action))

            elif itype in ("tool_call", "function_call"):
                parsed = self._parse_end_test(item)
                if parsed is not None:
                    items.append(parsed)

        return items

    def _parse_end_test(self, item: Dict[str, Any]) -> Optional[LLMOutputItem]:
        """Parse an end_test function/tool call from the OpenAI response."""
        try:
            tool_name = (
                (item.get("name") or item.get("tool_name") or "")
                or (item.get("tool", {}) or {}).get("function", {}).get("name")
                or (item.get("function", {}) or {}).get("name")
                or ""
            )
            tool_name = str(tool_name).strip().lower()
            if tool_name != "end_test":
                return None

            raw_args = item.get("arguments")
            args: Dict[str, Any]
            if isinstance(raw_args, dict):
                args = raw_args
            elif isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except Exception:
                    args = {}
            elif isinstance(raw_args, list):
                parsed: Optional[Dict[str, Any]] = None
                for part in raw_args:
                    if isinstance(part, dict):
                        if part.get("type") in ("input_json", "json") and isinstance(part.get("json"), dict):
                            parsed = part.get("json")
                            break
                        if part.get("type") == "input_text" and isinstance(part.get("text"), str):
                            try:
                                parsed = json.loads(part.get("text") or "")
                                break
                            except Exception:
                                pass
                if parsed is None:
                    tool_obj = item.get("tool") or {}
                    fn_obj = (tool_obj.get("function") if isinstance(tool_obj, dict) else {}) or {}
                    nested_args = fn_obj.get("arguments")
                    if isinstance(nested_args, str):
                        try:
                            parsed = json.loads(nested_args)
                        except Exception:
                            parsed = None
                    elif isinstance(nested_args, dict):
                        parsed = nested_args
                args = parsed or {}
            else:
                args = {}

            success_flag = bool(args.get("success", False))
            return LLMOutputItem(type=LLMOutputType.END_TEST, success=success_flag)
        except Exception:
            return None
