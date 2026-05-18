import asyncio
import logging

from agents import DocsAgent, PerformanceAgent, SecurityAgent, TestsAgent
from config import MAX_DIFF_TOKENS
from database import save_review
from github_client import format_review_comment, post_review_comment

logger = logging.getLogger(__name__)


def _chunk_diff(diff: str, max_chars: int = MAX_DIFF_TOKENS * 4) -> str:
    """Truncate diff to stay within token budget (1 token ≈ 4 chars)."""
    if len(diff) <= max_chars:
        return diff
    logger.warning("Diff truncated from %d to %d chars", len(diff), max_chars)
    return diff[:max_chars] + "\n\n[... diff truncated to fit token limit ...]"


def _count_files(diff: str) -> int:
    return diff.count("--- a/")


async def _run_agent_safe(agent, diff: str, context: dict) -> dict:
    try:
        result = await agent.review(diff, context)
        logger.info("[%s] completed — %d finding(s)", agent.name, len(result.get("findings", [])))
        return result
    except Exception as exc:
        logger.error("[%s] failed: %s", agent.name, exc)
        return {
            "agent": agent.name,
            "findings": [],
            "severity": "low",
            "summary": f"Agent failed to complete review: {exc}",
        }


async def run_review(
    pr_number: int,
    repo_full_name: str,
    pr_title: str,
    base_branch: str,
    head_branch: str,
    diff: str,
) -> None:
    logger.info("Starting review for %s#%d", repo_full_name, pr_number)

    diff_chunk = _chunk_diff(diff)
    context = {
        "pr_title": pr_title,
        "repo_full_name": repo_full_name,
        "base_branch": base_branch,
        "head_branch": head_branch,
    }

    security, performance, docs, tests = await asyncio.gather(
        _run_agent_safe(SecurityAgent(), diff_chunk, context),
        _run_agent_safe(PerformanceAgent(), diff_chunk, context),
        _run_agent_safe(DocsAgent(), diff_chunk, context),
        _run_agent_safe(TestsAgent(), diff_chunk, context),
    )

    num_files = _count_files(diff)
    comment_body = format_review_comment(security, performance, docs, tests, num_files)

    comment_url: str | None = None
    try:
        comment_url = post_review_comment(repo_full_name, pr_number, comment_body)
    except Exception as exc:
        logger.error("Failed to post comment: %s", exc)

    flat_findings: list[dict] = []
    for agent_result in (security, performance, docs, tests):
        for finding in agent_result.get("findings", []):
            flat_findings.append({
                "agent": agent_result["agent"],
                "severity": finding["severity"],
                "issue": finding["issue"],
                "suggestion": finding["suggestion"],
            })

    try:
        save_review(
            repo=repo_full_name,
            pr_number=pr_number,
            pr_title=pr_title,
            total_findings=len(flat_findings),
            comment_url=comment_url,
            findings_data=flat_findings,
        )
    except Exception as exc:
        logger.error("Failed to save review to DB: %s", exc)

    logger.info(
        "Review complete for %s#%d — %d finding(s), comment: %s",
        repo_full_name, pr_number, len(flat_findings), comment_url,
    )
