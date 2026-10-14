"""Select the LLM implementation from settings (``LLM_PROVIDER=gemini|mock``)."""

from __future__ import annotations

import os

from app.config import Settings
from app.llm.base import LLMClient
from app.llm.gemini import GeminiClient
from app.llm.mock import MockLLMClient, parse_faults


def build_llm_client(settings: Settings) -> LLMClient:
    if settings.llm_provider == "mock":
        # Demo-only knobs, read straight from the environment so they cannot be set by accident
        # in a real deployment's Settings.
        return MockLLMClient(
            faults=parse_faults(os.environ.get("MOCK_LLM_FAULTS", "")),
            latency_seconds=float(os.environ.get("MOCK_LLM_LATENCY_SECONDS", "0")),
        )
    assert settings.gemini_api_key is not None  # enforced by Settings validation
    return GeminiClient(
        api_key=settings.gemini_api_key.get_secret_value(),
        model=settings.gemini_model,
        base_url=settings.gemini_base_url,
        temperature=settings.gemini_temperature,
        timeout_seconds=settings.gemini_request_timeout_seconds,
    )
