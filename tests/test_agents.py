"""Each agent's happy path through MockLLMClient, plus prompt construction and retry behaviour."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.agents import AGENT_REGISTRY, ReviewAgent, build_agents
from app.agents.base import JSON_REPAIR_HINT, OUTPUT_CONTRACT
from app.chunking import DiffChunk, plan_chunks
from app.config import Settings
from app.llm.base import LLMError, LLMRequest
from app.llm.mock import MockLLMClient
from app.models import AgentStatus, ChangedFile, PRContext, Severity


@pytest.fixture
def chunks(sample_files: list[ChangedFile]) -> list[DiffChunk]:
    return plan_chunks(sample_files, max_chars=24_000, max_chunks=40).chunks


@pytest.fixture
def all_files(sample_files: list[ChangedFile]) -> list[str]:
    return [f.filename for f in sample_files]


def make_agent(name: str, llm: MockLLMClient, **kw: float) -> ReviewAgent:
    params = {"timeout_seconds": 1.0, "max_retries": 1, "retry_backoff_seconds": 0.0, **kw}
    return AGENT_REGISTRY[name](llm, asyncio.Semaphore(4), **params)  # type: ignore[arg-type]


EXPECTED = {
    "security": {
        "Hardcoded secret in `API_KEY`",
        "SQL injection via string-built query",
        "Arbitrary code execution via `eval`/`exec`",
        "Predictable token from the `random` module",
    },
    "performance": {
        "N+1 query pattern: data access inside a loop",
        "Quadratic nested loop over the same collection",
        "Blocking call inside `async def`",
    },
    "documentation": {
        "Missing docstring on public function `calculate_score`",
        "Single-letter names in `calculate_score`",
    },
    "tests": {"No tests for `authenticate_user`", "No tests for `generate_report`"},
}


@pytest.mark.parametrize("name", sorted(AGENT_REGISTRY))
async def test_agent_happy_path(
    name: str, chunks: list[DiffChunk], pr_ctx: PRContext, all_files: list[str]
) -> None:
    llm = MockLLMClient()
    run = await make_agent(name, llm).run(chunks, pr_ctx, all_files)

    assert run.agent == name
    assert run.status is AgentStatus.OK
    assert run.error is None
    assert run.chunks_total == 3 and run.chunks_failed == 0
    assert run.attempts == 3  # one call per chunk, no retries needed
    assert run.token_estimate > 0
    assert EXPECTED[name] <= {f.title for f in run.findings}
    assert all(f.agent == name for f in run.findings)
    assert all(f.file in {c.filename for c in chunks} for f in run.findings)
    assert len(llm.calls) == 3 and {c.agent for c in llm.calls} == {name}


async def test_security_findings_point_at_the_right_lines(
    chunks: list[DiffChunk], pr_ctx: PRContext, all_files: list[str]
) -> None:
    run = await make_agent("security", MockLLMClient()).run(chunks, pr_ctx, all_files)
    located = {(f.file, f.line, f.severity) for f in run.findings}
    # Line numbers refer to the new file (see tests/fixtures/sample_pr.diff).
    assert ("app/auth.py", 7, Severity.CRITICAL) in located  # API_KEY = "sk-prod-..."
    assert ("app/auth.py", 12, Severity.CRITICAL) in located  # string-built SQL
    assert ("app/utils.py", 18, Severity.CRITICAL) in located  # eval(...)


async def test_tests_agent_is_quiet_when_pr_contains_tests(
    chunks: list[DiffChunk], pr_ctx: PRContext
) -> None:
    run = await make_agent("tests", MockLLMClient()).run(
        chunks, pr_ctx, ["app/auth.py", "tests/test_auth.py"]
    )
    assert run.findings == []


def test_system_prompts_are_distinct_and_carry_the_contract() -> None:
    llm = MockLLMClient()
    prompts = {name: make_agent(name, llm).system_prompt for name in AGENT_REGISTRY}
    assert len(set(prompts.values())) == 4
    for prompt in prompts.values():
        assert OUTPUT_CONTRACT in prompt
        assert "untrusted input" in prompt


def test_user_prompt_contents(chunks: list[DiffChunk], pr_ctx: PRContext) -> None:
    agent = make_agent("security", MockLLMClient())
    multi = DiffChunk(
        filename="x.py", status="added", content="@@ -0,0 +1 @@\n    1 + a", part=2, total_parts=3
    )
    prompt = agent.build_prompt(multi, pr_ctx, ["x.py", "y.py"])
    assert "Repository: acme/shop" in prompt
    assert "Pull request #42:" in prompt
    assert "Files changed in this PR: x.py, y.py" in prompt
    assert "File under review: x.py (added) — part 2 of 3" in prompt
    assert '<diff file="x.py">' in prompt and prompt.endswith("</diff>")


async def test_flaky_call_is_retried_once_and_succeeds(
    chunks: list[DiffChunk], pr_ctx: PRContext, all_files: list[str]
) -> None:
    llm = MockLLMClient(faults={"security": "flaky"})
    run = await make_agent("security", llm).run(chunks[:1], pr_ctx, all_files)
    assert run.status is AgentStatus.OK
    assert run.attempts == 2
    assert run.findings


async def test_invalid_json_retry_includes_repair_hint(
    chunks: list[DiffChunk], pr_ctx: PRContext, all_files: list[str]
) -> None:
    replies = iter(["not json at all", json.dumps({"findings": [], "summary": "ok"})])
    llm = MockLLMClient(responder=lambda _req: next(replies))
    run = await make_agent("documentation", llm).run(chunks[:1], pr_ctx, all_files)
    assert run.status is AgentStatus.OK and run.attempts == 2
    assert JSON_REPAIR_HINT not in llm.calls[0].prompt
    assert JSON_REPAIR_HINT in llm.calls[1].prompt


async def test_non_retryable_error_is_not_retried(
    chunks: list[DiffChunk], pr_ctx: PRContext, all_files: list[str]
) -> None:
    class AuthFailingLLM(MockLLMClient):
        async def complete(self, request: LLMRequest):  # type: ignore[override]
            self.calls.append(request)
            raise LLMError("HTTP 401: API key invalid", retryable=False)

    llm = AuthFailingLLM()
    run = await make_agent("security", llm).run(chunks[:1], pr_ctx, all_files)
    assert run.status is AgentStatus.DEGRADED
    assert len(llm.calls) == 1
    assert "API key invalid" in (run.error or "")


async def test_hallucinated_file_path_is_pinned_to_chunk(pr_ctx: PRContext) -> None:
    reply = {
        "findings": [{"file": "b/somewhere/else.py", "line": 1, "severity": "low", "title": "t"}]
    }
    llm = MockLLMClient(responder=lambda _req: json.dumps(reply))
    chunk = DiffChunk(filename="real.py", status="added", content="@@ -0,0 +1 @@\n    1 + x")
    run = await make_agent("security", llm).run([chunk], pr_ctx, ["real.py"])
    assert run.findings[0].file == "real.py"


async def test_partial_failure_keeps_successful_chunks(
    chunks: list[DiffChunk], pr_ctx: PRContext, all_files: list[str]
) -> None:
    """One chunk fails twice; the agent degrades but keeps findings from the other chunks."""

    def responder(req: LLMRequest) -> str:
        if "File under review: app/utils.py" in req.prompt:
            return "garbage"
        return MockLLMClient._heuristic(req)

    run = await make_agent("security", MockLLMClient(responder=responder)).run(
        chunks, pr_ctx, all_files
    )
    assert run.status is AgentStatus.DEGRADED
    assert run.chunks_failed == 1 and run.chunks_total == 3
    assert "app/utils.py" in (run.error or "")
    assert {f.file for f in run.findings} == {"app/auth.py"}  # reports.py has no security issue


async def test_semaphore_bounds_concurrent_llm_calls(
    chunks: list[DiffChunk], pr_ctx: PRContext, all_files: list[str]
) -> None:
    llm = MockLLMClient(latency_seconds=0.02)
    settings = Settings(
        _env_file=None, llm_provider="mock", llm_max_concurrency=2, agent_retry_backoff_seconds=0
    )
    agents = build_agents(llm, settings, asyncio.Semaphore(settings.llm_max_concurrency))
    await asyncio.gather(*(a.run(chunks, pr_ctx, all_files) for a in agents))
    assert len(llm.calls) == 12  # 4 agents x 3 chunks
    assert llm.max_in_flight == 2


def test_build_agents_rejects_unknown_names() -> None:
    settings = Settings(_env_file=None, llm_provider="mock", enabled_agents=["security", "style"])
    with pytest.raises(ValueError, match="style"):
        build_agents(MockLLMClient(), settings, asyncio.Semaphore(1))


def test_build_agents_respects_enabled_list() -> None:
    settings = Settings(_env_file=None, llm_provider="mock", enabled_agents=["tests", "security"])
    agents = build_agents(MockLLMClient(), settings, asyncio.Semaphore(1))
    assert [a.name for a in agents] == ["tests", "security"]
