"""Run all agents concurrently with per-agent fault isolation."""

from __future__ import annotations

import asyncio
import logging

from app.agents.base import ReviewAgent
from app.chunking import DiffChunk
from app.models import AgentRun, AgentStatus, PRContext

logger = logging.getLogger(__name__)


class AgentOrchestrator:
    """Fan the chunks out to every agent in parallel and collect one ``AgentRun`` per agent.

    There are two layers of isolation:
    1. Each agent handles its own expected failures (timeout, LLM error, bad JSON) and returns
       a DEGRADED ``AgentRun`` instead of raising.
    2. ``asyncio.gather(..., return_exceptions=True)`` makes sure that even an *unexpected*
       exception escaping an agent does not cancel its siblings. It is turned into a DEGRADED
       ``AgentRun`` here.
    """

    def __init__(self, agents: list[ReviewAgent]) -> None:
        if not agents:
            raise ValueError("at least one agent is required")
        self.agents = agents

    async def run(
        self, chunks: list[DiffChunk], ctx: PRContext, all_files: list[str]
    ) -> list[AgentRun]:
        results = await asyncio.gather(
            *(agent.run(chunks, ctx, all_files) for agent in self.agents),
            return_exceptions=True,
        )
        runs: list[AgentRun] = []
        for agent, result in zip(self.agents, results, strict=True):
            if isinstance(result, BaseException):
                logger.error(
                    "agent crashed",
                    extra={"agent": agent.name, "error": f"{type(result).__name__}: {result}"},
                    exc_info=result if isinstance(result, Exception) else None,
                )
                runs.append(
                    AgentRun(
                        agent=agent.name,
                        status=AgentStatus.DEGRADED,
                        error=f"agent crashed: {type(result).__name__}: {result}",
                        chunks_total=len(chunks),
                        chunks_failed=len(chunks),
                    )
                )
            else:
                logger.info(
                    "agent finished",
                    extra={
                        "agent": agent.name,
                        "status": result.status.value,
                        "findings": len(result.findings),
                        "latency_ms": result.latency_ms,
                    },
                )
                runs.append(result)
        return runs
