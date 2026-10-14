"""SQLite persistence for reviews, agent runs, and findings.

The stdlib ``sqlite3`` driver is blocking, so every public method runs its work in a thread
via ``asyncio.to_thread`` and opens its own short-lived connection (``sqlite3`` connections
should not be shared across threads). WAL mode lets readers run during a write.

Idempotency: ``reviews.delivery_id`` is ``UNIQUE``. ``claim_delivery`` inserts a row for each
``X-GitHub-Delivery``, and the database (not application code) decides atomically whether the
delivery is new, so two concurrent duplicate deliveries cannot both win.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.models import PRContext, ReviewResult

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id     TEXT    NOT NULL UNIQUE,
    repo            TEXT    NOT NULL,
    pr_number       INTEGER NOT NULL,
    pr_title        TEXT    NOT NULL DEFAULT '',
    head_sha        TEXT    NOT NULL DEFAULT '',
    action          TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL
                    CHECK (status IN ('queued', 'running', 'completed', 'failed', 'superseded')),
    total_findings  INTEGER NOT NULL DEFAULT 0,
    files_reviewed  INTEGER NOT NULL DEFAULT 0,
    degraded_agents TEXT    NOT NULL DEFAULT '[]',
    skipped_files   TEXT    NOT NULL DEFAULT '[]',
    markdown        TEXT,
    comment_url     TEXT,
    error           TEXT,
    attempts        INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    completed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_repo_pr ON reviews (repo, pr_number, id DESC);

CREATE TABLE IF NOT EXISTS agent_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id       INTEGER NOT NULL REFERENCES reviews (id) ON DELETE CASCADE,
    agent           TEXT    NOT NULL,
    status          TEXT    NOT NULL,
    latency_ms      INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    token_estimate  INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    chunks_total    INTEGER NOT NULL DEFAULT 0,
    chunks_failed   INTEGER NOT NULL DEFAULT 0,
    findings_count  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_review ON agent_runs (review_id);

CREATE TABLE IF NOT EXISTS findings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id       INTEGER NOT NULL REFERENCES reviews (id) ON DELETE CASCADE,
    agent           TEXT    NOT NULL,
    file            TEXT    NOT NULL,
    line            INTEGER,
    severity        TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    explanation     TEXT    NOT NULL DEFAULT '',
    suggestion      TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_findings_review ON findings (review_id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class DeliveryClaim:
    review_id: int
    is_new: bool  # False means duplicate: do not process again
    status: str


class ReviewRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ schema / lifecycle

    def _init_sync(self) -> int:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < SCHEMA_VERSION:
                self._migrate_legacy(conn)
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            # A review left queued/running means the process died mid-review. Mark it failed
            # so that a GitHub redelivery of the same event is allowed to retry it.
            cur = conn.execute(
                "UPDATE reviews SET status = 'failed', error = 'interrupted by restart', "
                "updated_at = ? WHERE status IN ('queued', 'running')",
                (_now(),),
            )
            return cur.rowcount

    @staticmethod
    def _migrate_legacy(conn: sqlite3.Connection) -> None:
        """v1 (the original app) had ``reviews``/``findings`` tables with another shape.

        Rename them instead of dropping them, so no existing data is lost."""
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "reviews" not in tables:
            return
        columns = {r[1] for r in conn.execute("PRAGMA table_info(reviews)")}
        if "delivery_id" in columns:
            return
        logger.warning("migrating legacy v1 schema: renaming tables to legacy_*_v1")
        conn.execute("ALTER TABLE reviews RENAME TO legacy_reviews_v1")
        if "findings" in tables:
            conn.execute("ALTER TABLE findings RENAME TO legacy_findings_v1")

    async def init(self) -> None:
        recovered = await asyncio.to_thread(self._init_sync)
        if recovered:
            logger.warning("marked interrupted reviews as failed", extra={"count": recovered})

    async def ping(self) -> bool:
        def _ping() -> bool:
            with self._connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return True

        try:
            return await asyncio.to_thread(_ping)
        except sqlite3.Error:
            return False

    # ------------------------------------------------------------------ writes

    def _claim_sync(self, delivery_id: str, ctx: PRContext, action: str) -> DeliveryClaim:
        now = _now()
        with self._connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO reviews (delivery_id, repo, pr_number, pr_title, head_sha, action,"
                    " status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                    (delivery_id, ctx.repo_full_name, ctx.pr_number, ctx.title, ctx.head_sha,
                     action, now, now),
                )  # fmt: skip
                assert cur.lastrowid is not None
                return DeliveryClaim(review_id=cur.lastrowid, is_new=True, status="queued")
            except sqlite3.IntegrityError:
                pass
            # Duplicate delivery. Only a previously *failed* review may be re-queued, and the
            # conditional UPDATE makes that atomic as well.
            cur = conn.execute(
                "UPDATE reviews SET status = 'queued', error = NULL, attempts = attempts + 1, "
                "updated_at = ? WHERE delivery_id = ? AND status = 'failed'",
                (now, delivery_id),
            )
            row = conn.execute(
                "SELECT id, status FROM reviews WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            return DeliveryClaim(
                review_id=row["id"], is_new=cur.rowcount == 1, status=row["status"]
            )

    async def claim_delivery(self, delivery_id: str, ctx: PRContext, action: str) -> DeliveryClaim:
        """Atomically register a webhook delivery. ``is_new=False`` means it is a duplicate."""
        return await asyncio.to_thread(self._claim_sync, delivery_id, ctx, action)

    async def set_status(self, review_id: int, status: str, error: str | None = None) -> None:
        def _set() -> None:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE reviews SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                    (status, error, _now(), review_id),
                )

        await asyncio.to_thread(_set)

    def _save_result_sync(
        self, review_id: int, result: ReviewResult, status: str, comment_url: str | None,
        error: str | None,
    ) -> None:  # fmt: skip
        now = _now()
        with self._connect() as conn:
            # Re-processing a failed delivery replaces its earlier partial results.
            conn.execute("DELETE FROM agent_runs WHERE review_id = ?", (review_id,))
            conn.execute("DELETE FROM findings WHERE review_id = ?", (review_id,))
            conn.executemany(
                "INSERT INTO agent_runs (review_id, agent, status, latency_ms, error,"
                " token_estimate, attempts, chunks_total, chunks_failed, findings_count)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (review_id, r.agent, r.status.value, r.latency_ms, r.error, r.token_estimate,
                     r.attempts, r.chunks_total, r.chunks_failed, len(r.findings))
                    for r in result.agent_runs
                ],
            )  # fmt: skip
            conn.executemany(
                "INSERT INTO findings (review_id, agent, file, line, severity, title, explanation,"
                " suggestion) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (review_id, f.agent, f.file, f.line, f.severity.value, f.title, f.explanation,
                     f.suggestion)
                    for f in result.findings
                ],
            )  # fmt: skip
            conn.execute(
                "UPDATE reviews SET status = ?, total_findings = ?, files_reviewed = ?,"
                " degraded_agents = ?, skipped_files = ?, markdown = ?, comment_url = ?,"
                " error = ?, updated_at = ?, completed_at = ? WHERE id = ?",
                (
                    status,
                    len(result.findings),
                    result.files_reviewed,
                    json.dumps([r.agent for r in result.degraded_agents]),
                    json.dumps([s.model_dump() for s in result.skipped_files]),
                    result.markdown,
                    comment_url,
                    error,
                    now,
                    now,
                    review_id,
                ),
            )

    async def save_result(
        self,
        review_id: int,
        result: ReviewResult,
        *,
        status: str = "completed",
        comment_url: str | None = None,
        error: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._save_result_sync, review_id, result, status, comment_url, error
        )

    # ------------------------------------------------------------------ reads

    @staticmethod
    def _review_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["degraded_agents"] = json.loads(data["degraded_agents"])
        data["skipped_files"] = json.loads(data["skipped_files"])
        return data

    async def list_reviews(
        self, *, limit: int = 20, offset: int = 0, repo: str | None = None,
        pr_number: int | None = None,
    ) -> list[dict[str, Any]]:  # fmt: skip
        def _list() -> list[dict[str, Any]]:
            # Static SQL: optional filters are expressed as "? IS NULL OR column = ?".
            query = (
                "SELECT id, delivery_id, repo, pr_number, pr_title, head_sha, action, status,"
                " total_findings, files_reviewed, degraded_agents, skipped_files, comment_url,"
                " error, attempts, created_at, updated_at, completed_at FROM reviews"
                " WHERE (? IS NULL OR repo = ?) AND (? IS NULL OR pr_number = ?)"
                " ORDER BY id DESC LIMIT ? OFFSET ?"
            )
            params = (repo, repo, pr_number, pr_number, limit, offset)
            with self._connect() as conn:
                rows = conn.execute(query, params).fetchall()
            return [self._review_dict(r) for r in rows]

        return await asyncio.to_thread(_list)

    async def get_review(self, review_id: int) -> dict[str, Any] | None:
        def _get() -> dict[str, Any] | None:
            with self._connect() as conn:
                row = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
                if row is None:
                    return None
                review = self._review_dict(row)
                review["agent_runs"] = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT agent, status, latency_ms, error, token_estimate, attempts,"
                        " chunks_total, chunks_failed, findings_count FROM agent_runs"
                        " WHERE review_id = ? ORDER BY id",
                        (review_id,),
                    )
                ]
                review["findings"] = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT agent, file, line, severity, title, explanation, suggestion"
                        " FROM findings WHERE review_id = ? ORDER BY id",
                        (review_id,),
                    )
                ]
            return review

        return await asyncio.to_thread(_get)
