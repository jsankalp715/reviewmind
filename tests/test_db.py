"""SQLite repository: idempotent claims, results, crash recovery, and legacy migration."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from app.db import SCHEMA_VERSION, ReviewRepository
from app.models import AgentRun, AgentStatus, Finding, PRContext, ReviewResult, SkippedFile

CTX = PRContext(repo_full_name="acme/shop", pr_number=7, title="t", head_sha="abc")


@pytest.fixture
async def repo(tmp_path: Path) -> ReviewRepository:
    r = ReviewRepository(tmp_path / "nested" / "db.sqlite")
    await r.init()
    return r


def result() -> ReviewResult:
    return ReviewResult(
        findings=[
            Finding(
                agent="security",
                file="a.py",
                line=3,
                severity="high",
                title="T",
                explanation="E",
                suggestion="S",
            )
        ],
        agent_runs=[
            AgentRun(
                agent="security",
                findings=[],
                latency_ms=12,
                attempts=1,
                chunks_total=1,
                token_estimate=99,
            ),
            AgentRun(
                agent="tests",
                status=AgentStatus.DEGRADED,
                error="timed out",
                attempts=2,
                chunks_total=1,
                chunks_failed=1,
            ),
        ],
        skipped_files=[SkippedFile(filename="x.lock", reason="lockfile")],
        files_reviewed=1,
        markdown="## md",
    )


async def test_claim_is_idempotent(repo: ReviewRepository) -> None:
    first = await repo.claim_delivery("d1", CTX, "opened")
    second = await repo.claim_delivery("d1", CTX, "opened")
    assert first.is_new and not second.is_new
    assert first.review_id == second.review_id
    assert second.status == "queued"


async def test_concurrent_claims_have_exactly_one_winner(repo: ReviewRepository) -> None:
    claims = await asyncio.gather(*(repo.claim_delivery("race", CTX, "opened") for _ in range(20)))
    assert sum(c.is_new for c in claims) == 1
    assert len({c.review_id for c in claims}) == 1


async def test_failed_review_can_be_reclaimed_once(repo: ReviewRepository) -> None:
    claim = await repo.claim_delivery("d1", CTX, "opened")
    await repo.set_status(claim.review_id, "failed", "boom")
    again = await asyncio.gather(*(repo.claim_delivery("d1", CTX, "opened") for _ in range(5)))
    assert sum(c.is_new for c in again) == 1
    review = await repo.get_review(claim.review_id)
    assert review is not None
    assert review["status"] == "queued" and review["error"] is None and review["attempts"] == 2


async def test_completed_review_is_not_reclaimed(repo: ReviewRepository) -> None:
    claim = await repo.claim_delivery("d1", CTX, "opened")
    await repo.save_result(claim.review_id, result(), comment_url="https://x")
    assert not (await repo.claim_delivery("d1", CTX, "opened")).is_new


async def test_save_and_read_back(repo: ReviewRepository) -> None:
    claim = await repo.claim_delivery("d1", CTX, "opened")
    await repo.save_result(claim.review_id, result(), comment_url="https://x")
    review = await repo.get_review(claim.review_id)
    assert review is not None
    assert review["status"] == "completed"
    assert review["comment_url"] == "https://x"
    assert review["total_findings"] == 1 and review["files_reviewed"] == 1
    assert review["degraded_agents"] == ["tests"]
    assert review["skipped_files"] == [{"filename": "x.lock", "reason": "lockfile"}]
    assert review["markdown"] == "## md" and review["completed_at"]
    runs = {r["agent"]: r for r in review["agent_runs"]}
    assert runs["security"] == {
        "agent": "security",
        "status": "ok",
        "latency_ms": 12,
        "error": None,
        "token_estimate": 99,
        "attempts": 1,
        "chunks_total": 1,
        "chunks_failed": 0,
        "findings_count": 0,
    }
    assert runs["tests"]["status"] == "degraded" and runs["tests"]["error"] == "timed out"
    assert review["findings"] == [
        {
            "agent": "security",
            "file": "a.py",
            "line": 3,
            "severity": "high",
            "title": "T",
            "explanation": "E",
            "suggestion": "S",
        }
    ]


async def test_saving_twice_replaces_children(repo: ReviewRepository) -> None:
    claim = await repo.claim_delivery("d1", CTX, "opened")
    await repo.save_result(claim.review_id, result(), status="failed", error="post failed")
    await repo.save_result(claim.review_id, result())
    review = await repo.get_review(claim.review_id)
    assert review is not None
    assert len(review["agent_runs"]) == 2 and len(review["findings"]) == 1
    assert review["error"] is None


async def test_list_and_filter(repo: ReviewRepository) -> None:
    for i in range(3):
        await repo.claim_delivery(f"d{i}", CTX, "opened")
    other = CTX.model_copy(update={"repo_full_name": "acme/other", "pr_number": 1})
    await repo.claim_delivery("o1", other, "opened")
    assert [r["delivery_id"] for r in await repo.list_reviews()] == ["o1", "d2", "d1", "d0"]
    assert len(await repo.list_reviews(repo="acme/shop")) == 3
    assert len(await repo.list_reviews(repo="acme/shop", pr_number=99)) == 0
    assert [r["delivery_id"] for r in await repo.list_reviews(limit=2, offset=1)] == ["d2", "d1"]
    assert await repo.get_review(9999) is None


async def test_interrupted_reviews_are_marked_failed_on_restart(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    repo = ReviewRepository(path)
    await repo.init()
    claim = await repo.claim_delivery("d1", CTX, "opened")
    await repo.set_status(claim.review_id, "running")

    restarted = ReviewRepository(path)
    await restarted.init()
    review = await restarted.get_review(claim.review_id)
    assert review is not None
    assert review["status"] == "failed" and review["error"] == "interrupted by restart"
    assert (await restarted.claim_delivery("d1", CTX, "opened")).is_new  # retry allowed


async def test_legacy_v1_schema_is_preserved_not_dropped(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL,
            pr_number INTEGER NOT NULL, pr_title TEXT, timestamp TEXT NOT NULL,
            total_findings INTEGER DEFAULT 0, comment_url TEXT);
        CREATE TABLE findings (id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER NOT NULL,
            agent TEXT NOT NULL, severity TEXT NOT NULL, issue TEXT NOT NULL, suggestion TEXT);
        INSERT INTO reviews (repo, pr_number, pr_title, timestamp) VALUES ('a/b', 1, 'old', 'ts');
        """
    )
    conn.commit()
    conn.close()

    repo = ReviewRepository(path)
    await repo.init()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT pr_title FROM legacy_reviews_v1").fetchone() == ("old",)
    assert conn.execute("SELECT count(*) FROM legacy_findings_v1").fetchone() == (0,)
    assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
    conn.close()
    assert (await repo.claim_delivery("new", CTX, "opened")).is_new


async def test_ping(repo: ReviewRepository, tmp_path: Path) -> None:
    assert await repo.ping()
    broken = ReviewRepository(tmp_path / "missing-dir" / "x" / "db")
    # The directory does not exist and init() was never called, so connecting fails.
    assert not await broken.ping()
