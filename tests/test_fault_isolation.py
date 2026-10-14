"""Per-agent fault isolation: one agent raises, one times out, one returns garbage JSON.

The remaining agent must still succeed, and the posted review must say which agents degraded.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agents import AGENT_REGISTRY, ReviewAgent
from app.chunking import DiffChunk, plan_chunks
from app.llm.mock import MockLLMClient, parse_faults
from app.models import AgentRun, AgentStatus, ChangedFile, PRContext
from app.orchestrator import AgentOrchestrator
from tests.conftest import FakeGitHub, signed_headers

FAULTS = {"security": "error", "performance": "timeout", "documentation": "garbage"}


@pytest.fixture
def chunks(sample_files: list[ChangedFile]) -> list[DiffChunk]:
    return plan_chunks(sample_files, max_chars=24_000, max_chunks=40).chunks


def agents_for(llm: MockLLMClient, timeout: float = 0.2) -> list[ReviewAgent]:
    sem = asyncio.Semaphore(8)
    return [
        cls(llm, sem, timeout_seconds=timeout, max_retries=1, retry_backoff_seconds=0)
        for cls in AGENT_REGISTRY.values()
    ]


async def test_three_faulty_agents_do_not_affect_the_fourth(
    chunks: list[DiffChunk], pr_ctx: PRContext
) -> None:
    llm = MockLLMClient(faults=FAULTS)
    started = time.perf_counter()
    runs = await AgentOrchestrator(agents_for(llm)).run(chunks, pr_ctx, ["app/auth.py"])
    elapsed = time.perf_counter() - started

    by_agent = {r.agent: r for r in runs}
    assert [r.agent for r in runs] == ["security", "performance", "documentation", "tests"]

    assert by_agent["tests"].status is AgentStatus.OK
    assert by_agent["tests"].findings, "the healthy agent must still produce findings"

    for name in FAULTS:
        assert by_agent[name].status is AgentStatus.DEGRADED, name
        assert by_agent[name].findings == []
        assert by_agent[name].chunks_failed == len(chunks)
        assert by_agent[name].attempts == 2 * len(chunks), "each chunk: 1 try + 1 retry"

    assert "injected failure" in (by_agent["security"].error or "")
    assert "timed out after 0.2s" in (by_agent["performance"].error or "")
    assert "invalid JSON" in (by_agent["documentation"].error or "")

    # The timing-out agent is bounded: 2 attempts x 0.2s per chunk, chunks run in parallel.
    assert elapsed < 2.0


async def test_unexpected_crash_inside_agent_run_is_contained(
    chunks: list[DiffChunk], pr_ctx: PRContext
) -> None:
    """Even a bug that escapes an agent's own error handling must not cancel its siblings."""
    llm = MockLLMClient()
    agents = agents_for(llm)

    async def boom(*_a: Any, **_kw: Any) -> AgentRun:
        raise RuntimeError("bug in agent code")

    agents[0].run = boom  # type: ignore[method-assign]
    runs = await AgentOrchestrator(agents).run(chunks, pr_ctx, ["app/auth.py"])
    assert runs[0].status is AgentStatus.DEGRADED
    assert "agent crashed: RuntimeError: bug in agent code" in (runs[0].error or "")
    assert all(r.status is AgentStatus.OK for r in runs[1:])
    assert all(r.findings for r in runs[1:])


async def test_all_agents_failing_still_returns_four_runs(
    chunks: list[DiffChunk], pr_ctx: PRContext
) -> None:
    llm = MockLLMClient(faults={name: "error" for name in AGENT_REGISTRY})
    runs = await AgentOrchestrator(agents_for(llm)).run(chunks, pr_ctx, [])
    assert len(runs) == 4
    assert all(r.status is AgentStatus.DEGRADED for r in runs)


def test_orchestrator_requires_agents() -> None:
    with pytest.raises(ValueError):
        AgentOrchestrator([])


def test_parse_faults() -> None:
    assert parse_faults("tests:timeout, documentation:garbage") == {
        "tests": "timeout",
        "documentation": "garbage",
    }
    assert parse_faults("") == {}
    with pytest.raises(ValueError):
        parse_faults("tests:explode")


def test_degraded_agents_are_reported_in_the_posted_review(
    make_client: Any, mock_llm: MockLLMClient, pr_payload: dict[str, Any], fake_github: FakeGitHub
) -> None:
    """End to end through the webhook: the review is still posted and names the degraded agents."""
    mock_llm.faults.update(FAULTS)
    client: TestClient = make_client(agent_timeout_seconds=0.2)
    body = json.dumps(pr_payload).encode()
    resp = client.post("/webhook/github", content=body, headers=signed_headers(body))
    assert resp.status_code == 202

    assert len(fake_github.reviews) == 1
    markdown = fake_github.reviews[0]["body"]
    assert "**Partial review:**" in markdown
    assert "### ⚠️ Degraded agents" in markdown
    degraded_section = markdown.split("### ⚠️ Degraded agents", 1)[1]
    for label in ("🔒 Security", "⚡ Performance", "📝 Documentation"):
        assert label in degraded_section
    assert "🧪 Tests" not in degraded_section.split("<details>")[0]
    assert "### 🧪 Tests" in markdown and "No tests for `authenticate_user`" in markdown

    review = client.get(f"/reviews/{resp.json()['review_id']}").json()
    assert review["status"] == "completed"
    assert sorted(review["degraded_agents"]) == ["documentation", "performance", "security"]
    runs = {r["agent"]: r for r in review["agent_runs"]}
    assert runs["tests"]["status"] == "ok" and runs["tests"]["findings_count"] > 0
    assert runs["performance"]["status"] == "degraded"
    assert "timed out" in runs["performance"]["error"]
