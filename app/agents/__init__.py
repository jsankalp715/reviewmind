"""The four specialist review agents and a registry to build them by name."""

from __future__ import annotations

import asyncio

from app.agents.base import ReviewAgent
from app.agents.documentation import DocumentationAgent
from app.agents.performance import PerformanceAgent
from app.agents.security import SecurityAgent
from app.agents.tests import TestsAgent
from app.config import Settings
from app.llm.base import LLMClient

AGENT_REGISTRY: dict[str, type[ReviewAgent]] = {
    cls.name: cls for cls in (SecurityAgent, PerformanceAgent, DocumentationAgent, TestsAgent)
}


def build_agents(
    llm: LLMClient, settings: Settings, semaphore: asyncio.Semaphore
) -> list[ReviewAgent]:
    """Instantiate the enabled agents. All of them share one LLM concurrency semaphore."""
    unknown = set(settings.enabled_agents) - AGENT_REGISTRY.keys()
    if unknown:
        raise ValueError(f"unknown agent(s) in ENABLED_AGENTS: {sorted(unknown)}")
    return [
        AGENT_REGISTRY[name](
            llm,
            semaphore,
            timeout_seconds=settings.agent_timeout_seconds,
            max_retries=settings.agent_max_retries,
            retry_backoff_seconds=settings.agent_retry_backoff_seconds,
            chars_per_token=settings.chars_per_token,
        )
        for name in settings.enabled_agents
    ]


__all__ = [
    "AGENT_REGISTRY",
    "DocumentationAgent",
    "PerformanceAgent",
    "ReviewAgent",
    "SecurityAgent",
    "TestsAgent",
    "build_agents",
]
