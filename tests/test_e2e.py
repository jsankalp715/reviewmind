"""End to end: a signed webhook plus the sample diff produces rendered Markdown on the PR.

GitHub is mocked (``FakeGitHub``) and the LLM is ``MockLLMClient``. Everything else is real:
the FastAPI app, HMAC check, SQLite, chunking, agents, orchestrator, and aggregator.
"""

from __future__ import annotations

import io
import json
import logging
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.logging_config import JsonFormatter, TextFormatter, bind_log_context, configure_logging
from app.models import ChangedFile, PRContext
from tests.conftest import FakeGitHub, signed_headers


def send(client: TestClient, payload: dict[str, Any], delivery: str = "e2e-1") -> httpx.Response:
    body = json.dumps(payload).encode()
    headers = signed_headers(body, delivery=delivery)
    return client.post("/webhook/github", content=body, headers=headers)


def test_signed_webhook_to_posted_markdown(
    client: TestClient, pr_payload: dict[str, Any], fake_github: FakeGitHub
) -> None:
    resp = send(client, pr_payload)
    assert resp.status_code == 202
    review_id = resp.json()["review_id"]

    # GitHub calls: paginated file listing (5 files / 2 per page = 3 pages), head check, post.
    paths = [(r.method, r.url.path) for r in fake_github.requests]
    assert paths.count(("GET", "/repos/acme/shop/pulls/42/files")) == 3
    assert ("GET", "/repos/acme/shop/pulls/42") in paths
    assert paths[-1] == ("POST", "/repos/acme/shop/pulls/42/reviews")

    assert len(fake_github.reviews) == 1
    posted = fake_github.reviews[0]
    assert posted["event"] == "COMMENT"
    assert posted["commit_id"] == pr_payload["pull_request"]["head"]["sha"]
    md = posted["body"]

    # Structure
    assert md.startswith("## 🤖 ReviewMind review")
    assert "across **3** reviewed file(s)" in md
    assert "| Agent | Status | Findings |" in md
    for heading in (
        "### 🔒 Security",
        "### ⚡ Performance",
        "### 📝 Documentation",
        "### 🧪 Tests",
    ):
        assert heading in md
    assert "Degraded agents" not in md
    assert "- `assets/logo.png` — binary file" in md
    assert "- `poetry.lock` — lockfile" in md

    # Content: the fixture's planted problems are found at the right places.
    assert "🔴 **CRITICAL** `app/auth.py:7` — **Hardcoded secret in `API_KEY`**" in md
    assert "`app/auth.py:12` — **SQL injection via string-built query**" in md
    assert "`app/utils.py:18` — **Arbitrary code execution via `eval`/`exec`**" in md
    assert "`app/reports.py:19` — **Quadratic nested loop over the same collection**" in md
    assert "No tests for `authenticate_user`" in md

    # Critical findings come before anything less severe.
    assert md.index("CRITICAL") < md.index("**HIGH**") < md.index("**MEDIUM**")

    # Persistence and read API
    review = client.get(f"/reviews/{review_id}").json()
    assert review["status"] == "completed"
    assert review["comment_url"].startswith("https://github.com/acme/shop/pull/42")
    assert review["markdown"] == md
    assert review["total_findings"] == len(review["findings"]) > 10
    assert {r["agent"] for r in review["agent_runs"]} == {
        "security",
        "performance",
        "documentation",
        "tests",
    }
    assert all(r["status"] == "ok" and r["chunks_total"] == 3 for r in review["agent_runs"])
    assert all(r["token_estimate"] > 0 for r in review["agent_runs"])

    listing = client.get("/reviews", params={"repo": "acme/shop"}).json()["reviews"]
    assert [r["id"] for r in listing] == [review_id]
    assert "markdown" not in listing[0]  # list view stays light


def test_superseded_head_is_not_posted(
    client: TestClient, pr_payload: dict[str, Any], fake_github: FakeGitHub
) -> None:
    fake_github.head_sha = "f" * 40  # someone pushed while we were reviewing
    review_id = send(client, pr_payload).json()["review_id"]
    assert fake_github.reviews == []
    review = client.get(f"/reviews/{review_id}").json()
    assert review["status"] == "superseded"
    assert review["markdown"]  # still stored for inspection


def test_github_fetch_failure_marks_review_failed(
    client: TestClient, pr_payload: dict[str, Any], fake_github: FakeGitHub
) -> None:
    fake_github.overrides[r"/repos/.+/pulls/\d+/files"] = lambda _r: httpx.Response(
        404, json={"message": "Not Found"}
    )
    review_id = send(client, pr_payload).json()["review_id"]
    review = client.get(f"/reviews/{review_id}").json()
    assert review["status"] == "failed"
    assert "HTTP 404" in review["error"]
    assert fake_github.reviews == []


def test_review_with_only_skipped_files(
    client: TestClient, pr_payload: dict[str, Any], fake_github: FakeGitHub
) -> None:
    fake_github.files = [
        {"filename": "yarn.lock", "status": "modified", "patch": "@@ -1 +1 @@\n+x"}
    ]
    send(client, pr_payload)
    assert "No reviewable changes" in fake_github.reviews[0]["body"]


def test_post_reviews_disabled(
    make_client: Any, pr_payload: dict[str, Any], fake_github: FakeGitHub
) -> None:
    client = make_client(post_reviews=False)
    review_id = send(client, pr_payload).json()["review_id"]
    assert fake_github.reviews == []
    assert client.get(f"/reviews/{review_id}").json()["status"] == "completed"


def test_large_file_patch_is_filled_from_full_diff(
    client: TestClient, pr_payload: dict[str, Any], fake_github: FakeGitHub
) -> None:
    """GitHub's files API omits `patch` for very large files; we fall back to the .diff."""
    for f in fake_github.files:
        if f["filename"] == "app/utils.py":
            del f["patch"]
    send(client, pr_payload)
    md = fake_github.reviews[0]["body"]
    assert "`app/utils.py:18` — **Arbitrary code execution" in md
    assert any("diff" in r.headers.get("accept", "") for r in fake_github.requests)


async def test_fetch_files_tolerates_diff_endpoint_failure(
    fake_github: FakeGitHub, pr_ctx: PRContext, settings: Any, mock_llm: Any
) -> None:
    from app.main import build_state

    fake_github.files = [{"filename": "big.py", "status": "modified"}]
    fake_github.overrides[r"/repos/.+/pulls/\d+"] = lambda _r: httpx.Response(
        406, json={"message": "diff too large"}
    )
    state = build_state(settings, llm=mock_llm, github=fake_github.client())
    files = await state.pipeline.fetch_files(pr_ctx)
    assert files == [ChangedFile(filename="big.py", status="modified")]


def test_health(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body == {
        "status": "ok",
        "version": "2.0.0",
        "llm_provider": "mock",
        "database": "ok",
        "webhook_secret_configured": True,
    }


def test_review_not_found(client: TestClient) -> None:
    assert client.get("/reviews/12345").status_code == 404
    assert client.get("/reviews", params={"limit": 0}).status_code == 422


def test_structured_json_logs() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("reviewmind.test")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        token = bind_log_context(delivery_id="abc", review_id=None)
        log.info("hello %s", "world", extra={"agent": "security"})
        try:
            raise ValueError("bad")
        except ValueError:
            log.exception("failed")
        from app.logging_config import log_context

        log_context.reset(token)
    finally:
        log.removeHandler(handler)
    lines = [ln for ln in stream.getvalue().splitlines() if ln.startswith("{")]
    first, second = (json.loads(ln) for ln in lines)
    assert first["msg"] == "hello world" and first["level"] == "INFO"
    assert first["delivery_id"] == "abc" and first["agent"] == "security"
    assert "review_id" not in first
    assert "ValueError: bad" in second["exc_info"]


def test_text_log_format_and_configure() -> None:
    record = logging.makeLogRecord(
        {"msg": "hi", "levelname": "INFO", "name": "x", "agent": "tests"}
    )
    assert TextFormatter().format(record).endswith("hi agent=tests")
    configure_logging("WARNING", "text")
    assert logging.getLogger().level == logging.WARNING
    configure_logging("INFO", "json")
    assert isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)
