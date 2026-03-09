"""OpenAI provider for the GA computer tool flow on the Responses API."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI

from .base import LLMOutputItem, LLMOutputType, LLMProvider, LLMTurnResult


DEFAULT_MODEL = os.environ.get("OPENAI_COMPUTER_MODEL", "gpt-5.4")


class OpenAIProvider(LLMProvider):
    def __init__(self, model: Optional[str] = None, **kwargs: Any):
        self._client = OpenAI(**kwargs)
        self._model = model or DEFAULT_MODEL
        self._previous_response_id: Optional[str] = None
        self._pending_computer_call_id: Optional[str] = None
        self._conversation_anchor: Optional[str] = None
        self._system_text: Optional[str] = None

    def format_system_message(self, text: str) -> Dict[str, Any]:
        return {
            "role": "system",
            "content": [{"type": "input_text", "text": text}],
        }

    def format_user_message(self, text_parts: List[str], screenshot_data_url: Optional[str] = None) -> Dict[str, Any]:
        content: List[Dict[str, Any]] = [{"type": "input_text", "text": t} for t in text_parts]
        if screenshot_data_url:
            content.append({"type": "input_image", "image_url": screenshot_data_url, "detail": "original"})
        return {"role": "user", "content": content}

    def create_turn(
        self,
        input_messages: List[Dict[str, Any]],
        display_width: int,
        display_height: int,
    ) -> LLMTurnResult:
        del display_width, display_height

        system_text = self._extract_primary_text(input_messages[0] if input_messages else {})
        user_msg = input_messages[1] if len(input_messages) > 1 else {}
        conversation_anchor = self._extract_primary_text(user_msg)

        if self._should_reset(system_text, conversation_anchor):
            self._reset_conversation(system_text, conversation_anchor)

        request: Dict[str, Any] = {
            "model": self._model,
            "tools": self._build_tools(),
            "parallel_tool_calls": False,
            "reasoning": {"summary": "concise"},
            "truncation": "auto",
        }

        if self._pending_computer_call_id:
            if not self._previous_response_id:
                raise RuntimeError("Missing previous_response_id for computer_call_output")

            screenshot_url = self._extract_screenshot_url(user_msg)
            if not screenshot_url:
                raise RuntimeError("Missing screenshot for computer_call_output")

            request["previous_response_id"] = self._previous_response_id
            request["input"] = [
                {
                    "type": "computer_call_output",
                    "call_id": self._pending_computer_call_id,
                    "output": {
                        "type": "computer_screenshot",
                        "image_url": screenshot_url,
                        "detail": "original",
                    },
                }
            ]
        else:
            request["input"] = input_messages

        resp = self._client.responses.create(**request)
        resp_dict = resp.model_dump()
        items, pending_call_id, terminal = self._parse_outputs(resp_dict.get("output", []))
        if not terminal and not pending_call_id:
            raise RuntimeError("OpenAI response did not include a computer call id")
        self._previous_response_id = resp_dict.get("id")
        self._pending_computer_call_id = pending_call_id

        raw_json: Optional[str] = None
        try:
            raw_json = resp.model_dump_json()
        except Exception:
            pass

        return LLMTurnResult(
            items=items,
            raw_response={"dict": resp_dict, "json_str": raw_json},
            terminal=terminal,
        )

    def _build_tools(self) -> List[Dict[str, Any]]:
        return [{"type": "computer"}]

    def _should_reset(self, system_text: str, conversation_anchor: str) -> bool:
        if self._previous_response_id is None:
            return True
        if system_text != (self._system_text or ""):
            return True
        if conversation_anchor != (self._conversation_anchor or ""):
            return True
        return False

    def _reset_conversation(self, system_text: str, conversation_anchor: str) -> None:
        self._system_text = system_text
        self._conversation_anchor = conversation_anchor
        self._previous_response_id = None
        self._pending_computer_call_id = None

    def _extract_primary_text(self, message: Dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
        return ""

    def _extract_screenshot_url(self, message: Dict[str, Any]) -> Optional[str]:
        content = message.get("content")
        if not isinstance(content, list):
            return None
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "input_image":
                continue
            image_url = part.get("image_url")
            if isinstance(image_url, str) and image_url:
                return image_url
        return None

    def _parse_outputs(self, outputs: List[Dict[str, Any]]) -> Tuple[List[LLMOutputItem], Optional[str], bool]:
        items: List[LLMOutputItem] = []
        pending_call_id: Optional[str] = None
        saw_computer_call = False

        for item in outputs:
            itype = item.get("type")

            if itype == "reasoning":
                for part in (item.get("summary") or []):
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") != "summary_text":
                        continue
                    text = part.get("text", "")
                    if text:
                        items.append(LLMOutputItem(type=LLMOutputType.REASONING, text=text))

            elif itype == "message":
                for part in (item.get("content") or []):
                    if not isinstance(part, dict):
                        continue
                    text = self._extract_message_text(part)
                    if text:
                        items.append(LLMOutputItem(type=LLMOutputType.REASONING, text=text))

            elif itype == "computer_call":
                saw_computer_call = True
                pending_call_id = self._parse_call_id(item)
                for action in (item.get("actions") or []):
                    if isinstance(action, dict):
                        items.append(LLMOutputItem(type=LLMOutputType.COMPUTER_ACTION, action=dict(action)))

        if saw_computer_call:
            return items, pending_call_id, False

        return items, None, True

    def _extract_message_text(self, part: Dict[str, Any]) -> str:
        ptype = part.get("type")
        if ptype == "output_text":
            text = part.get("text")
            return text if isinstance(text, str) else ""
        if ptype == "refusal":
            text = part.get("refusal") or part.get("text")
            return text if isinstance(text, str) else ""
        return ""

    def _parse_call_id(self, item: Dict[str, Any]) -> Optional[str]:
        call_id = item.get("call_id")
        if isinstance(call_id, str) and call_id:
            return call_id
        return None
