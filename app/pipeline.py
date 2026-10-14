"""The end-to-end review pipeline: fetch → chunk → agents → aggregate → post → persist."""

from __future__ import annotations

import logging
import time

from app.aggregator import aggregate
from app.chunking import parse_unified_diff, plan_chunks, skip_reason
from app.config import Settings
from app.db import ReviewRepository
from app.github.client import GitHubAPIError, GitHubClient
from app.logging_config import bind_log_context
from app.models import ChangedFile, PRContext, ReviewResult
from app.orchestrator import AgentOrchestrator

logger = logging.getLogger(__name__)


class ReviewPipeline:
    def __init__(
        self,
        settings: Settings,
        github: GitHubClient,
        orchestrator: AgentOrchestrator,
        repository: ReviewRepository,
    ) -> None:
        self.settings = settings
        self.github = github
        self.orchestrator = orchestrator
        self.repository = repository

    async def fetch_files(self, ctx: PRContext) -> list[ChangedFile]:
        """List the PR's files. GitHub omits ``patch`` for very large text files, so fill those
        in from the full ``.diff`` when possible."""
        files = await self.github.list_pull_request_files(ctx.owner, ctx.repo, ctx.pr_number)
        # Files with no patch that *would* be reviewed if they had one (i.e. not binaries,
        # lockfiles, deletions, ...).
        missing = {
            f.filename
            for f in files
            if f.patch is None and skip_reason(f.model_copy(update={"patch": "@@"})) is None
        }
        if not missing:
            return files
        try:
            diff = await self.github.get_pull_request_diff(ctx.owner, ctx.repo, ctx.pr_number)
        except GitHubAPIError as exc:
            logger.warning(
                "could not fetch full diff for patch-less files", extra={"error": str(exc)}
            )
            return files
        by_name = {f.filename: f for f in parse_unified_diff(diff)}
        return [
            f.model_copy(update={"patch": by_name[f.filename].patch})
            if f.filename in missing and f.filename in by_name
            else f
            for f in files
        ]

    async def review(self, ctx: PRContext, files: list[ChangedFile]) -> ReviewResult:
        """Chunk the files, run every agent, and aggregate. Pure apart from LLM calls."""
        plan = plan_chunks(
            files,
            max_chars=self.settings.chunk_max_chars,
            max_chunks=self.settings.max_chunks_per_review,
        )
        logger.info(
            "chunk plan",
            extra={"chunks": len(plan.chunks), "files": plan.files_reviewed,
                   "skipped": len(plan.skipped)},
        )  # fmt: skip
        all_files = [f.filename for f in files]
        runs = await self.orchestrator.run(plan.chunks, ctx, all_files) if plan.chunks else []
        return aggregate(runs, plan.skipped, plan.files_reviewed, head_sha=ctx.head_sha)

    async def run(self, review_id: int, ctx: PRContext) -> ReviewResult | None:
        """Background-task entry point. Never raises; the outcome is recorded in the database."""
        bind_log_context(review_id=review_id, repo=ctx.repo_full_name, pr=ctx.pr_number)
        try:
            return await self._run(review_id, ctx)
        except Exception:
            # Last resort (e.g. the database itself failed). The row stays queued/running and
            # is marked failed at the next startup, so a redelivery can retry it.
            logger.exception("review crashed")
            return None

    async def _run(self, review_id: int, ctx: PRContext) -> ReviewResult | None:
        started = time.perf_counter()
        await self.repository.set_status(review_id, "running")
        try:
            files = await self.fetch_files(ctx)
            result = await self.review(ctx, files)
        except Exception as exc:
            logger.exception("review failed before posting")
            await self.repository.set_status(review_id, "failed", f"{type(exc).__name__}: {exc}")
            return None

        status, comment_url, error = "completed", None, None
        if self.settings.post_reviews:
            try:
                if await self._is_superseded(ctx):
                    status, error = "superseded", "PR head moved during review; not posted"
                else:
                    comment_url = await self.github.post_review(
                        ctx.owner, ctx.repo, ctx.pr_number, result.markdown, ctx.head_sha or None
                    )
            except Exception as exc:
                logger.exception("failed to post review")
                # Mark failed (but keep the results) so a redelivery is allowed to retry.
                status, error = "failed", f"posting review failed: {type(exc).__name__}: {exc}"

        await self.repository.save_result(
            review_id, result, status=status, comment_url=comment_url, error=error
        )
        logger.info(
            "review finished",
            extra={
                "status": status,
                "findings": len(result.findings),
                "degraded": [r.agent for r in result.degraded_agents],
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "comment_url": comment_url,
            },
        )
        return result

    async def _is_superseded(self, ctx: PRContext) -> bool:
        """True if new commits were pushed while we reviewed. The newer ``synchronize`` event
        gets its own review, so posting this stale one would only add noise."""
        if not ctx.head_sha:
            return False
        try:
            pr = await self.github.get_pull_request(ctx.owner, ctx.repo, ctx.pr_number)
        except GitHubAPIError:
            return False  # if we can't tell, post anyway
        current = (pr.get("head") or {}).get("sha")
        return bool(current) and current != ctx.head_sha
