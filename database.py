import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = "reviewmind.db"


def init_db() -> None:
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS reviews (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                repo        TEXT NOT NULL,
                pr_number   INTEGER NOT NULL,
                pr_title    TEXT,
                timestamp   TEXT NOT NULL,
                total_findings INTEGER DEFAULT 0,
                comment_url TEXT
            );

            CREATE TABLE IF NOT EXISTS findings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                review_id   INTEGER NOT NULL REFERENCES reviews(id),
                agent       TEXT NOT NULL,
                severity    TEXT NOT NULL,
                issue       TEXT NOT NULL,
                suggestion  TEXT
            );
        """)
    logger.info("Database initialised at %s", DB_PATH)


@contextmanager
def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_review(
    repo: str,
    pr_number: int,
    pr_title: str,
    total_findings: int,
    comment_url: Optional[str],
    findings_data: list[dict],
) -> int:
    timestamp = datetime.utcnow().isoformat()
    with _get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO reviews (repo, pr_number, pr_title, timestamp, total_findings, comment_url)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (repo, pr_number, pr_title, timestamp, total_findings, comment_url),
        )
        review_id: int = cur.lastrowid
        conn.executemany(
            """INSERT INTO findings (review_id, agent, severity, issue, suggestion)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (review_id, f["agent"], f["severity"], f["issue"], f["suggestion"])
                for f in findings_data
            ],
        )
    logger.info("Saved review id=%d for %s#%d", review_id, repo, pr_number)
    return review_id


def get_review_history(repo_name: str, limit: int = 10) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM reviews WHERE repo = ?
               ORDER BY timestamp DESC LIMIT ?""",
            (repo_name, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_review_detail(repo_name: str, pr_number: int) -> Optional[dict]:
    with _get_conn() as conn:
        review = conn.execute(
            "SELECT * FROM reviews WHERE repo = ? AND pr_number = ?",
            (repo_name, pr_number),
        ).fetchone()
        if not review:
            return None
        review_dict = dict(review)
        findings = conn.execute(
            "SELECT * FROM findings WHERE review_id = ?", (review_dict["id"],)
        ).fetchall()
        review_dict["findings"] = [dict(f) for f in findings]
    return review_dict
