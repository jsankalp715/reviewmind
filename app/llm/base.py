"""The provider-agnostic LLM interface the agents depend on."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class LLMRequest:
    """One completion request. ``agent`` is metadata used for logging and by the mock."""

    system: str
    prompt: str
    agent: str = ""
    json_mode: bool = True


@dataclass(frozen=True)
class LLMResponse:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)


class LLMError(Exception):
    """A provider failure. ``retryable=False`` means retrying cannot help (bad key, bad request)."""

    def __init__(self, message: str, *, retryable: bool = True, status_code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class LLMClient(ABC):
    """Anything that can turn a system prompt plus a user prompt into text."""

    name: str = "base"

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Return the model's text response, or raise ``LLMError``."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook, a no-op by default
        """Release network resources, if any."""
