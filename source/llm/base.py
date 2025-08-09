"""Abstract base class and shared types for LLM providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class LLMOutputType(Enum):
    REASONING = "reasoning"
    COMPUTER_ACTION = "computer_action"
    END_TEST = "end_test"


@dataclass
class LLMOutputItem:
    type: LLMOutputType
    text: Optional[str] = None
    action: Optional[Dict[str, Any]] = None
    success: Optional[bool] = None


@dataclass
class LLMTurnResult:
    items: List[LLMOutputItem] = field(default_factory=list)
    raw_response: Any = None


class LLMProvider(ABC):
    """Abstract interface that every LLM backend must implement."""

    @abstractmethod
    def create_turn(
        self,
        input_messages: List[Dict[str, Any]],
        display_width: int,
        display_height: int,
    ) -> LLMTurnResult:
        """Send messages + screenshot to the model and return parsed output."""
        ...

    @abstractmethod
    def format_system_message(self, text: str) -> Dict[str, Any]:
        """Build a provider-specific system message dict."""
        ...

    @abstractmethod
    def format_user_message(self, text_parts: List[str], screenshot_data_url: Optional[str] = None) -> Dict[str, Any]:
        """Build a provider-specific user message dict with optional screenshot."""
        ...


def create_provider(name: str = "openai", **kwargs: Any) -> LLMProvider:
    """Factory function. Instantiate an LLM provider by name.

    Supported values: "openai" (default), "claude".
    """
    name = name.lower().strip()
    if name == "openai":
        from .openai_provider import OpenAIProvider
        return OpenAIProvider(**kwargs)
    if name == "claude":
        from .claude_provider import ClaudeProvider
        return ClaudeProvider(**kwargs)
    raise ValueError(f"Unknown LLM provider: {name!r}. Supported: openai, claude")
