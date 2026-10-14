"""Read-API auth, payload size limit, settings parsing, and pipeline crash containment."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.chunking import chunk_file
from app.config import Settings
from app.main import build_state
from app.models import ChangedFile, PRContext
from tests.conftest import FakeGitHub, signed_headers


def test_reviews_require_token_when_configured(make_client: Any) -> None:
    client: TestClient = make_client(api_token="read-secret")
    assert client.get("/reviews").status_code == 401
    assert client.get("/reviews/1").status_code == 401
    assert client.get("/reviews", headers={"Authorization": "Bearer nope"}).status_code == 401
    ok = client.get("/reviews", headers={"Authorization": "Bearer read-secret"})
    assert ok.status_code == 200
    assert client.get("/health").status_code == 200  # health stays public


def test_reviews_open_when_no_token(client: TestClient) -> None:
    assert client.get("/reviews").status_code == 200


def test_oversized_webhook_rejected(make_client: Any, pr_payload: dict[str, Any]) -> None:
    client: TestClient = make_client(max_webhook_body_bytes=2048)
    pr_payload["padding"] = "x" * 4096
    body = json.dumps(pr_payload).encode()
    resp = client.post("/webhook/github", content=body, headers=signed_headers(body))
    assert resp.status_code == 413


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("security,tests", ["security", "tests"]),
        (" security , documentation ", ["security", "documentation"]),
        ('["performance"]', ["performance"]),
    ],
)
def test_enabled_agents_env_formats(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[str]
) -> None:
    monkeypatch.setenv("ENABLED_AGENTS", raw)
    assert Settings(_env_file=None, llm_provider="mock").enabled_agents == expected


def test_very_long_hunk_header_still_respects_budget() -> None:
    header = "@@ -1,300 +1,300 @@ def " + "f" * 2000 + "():"
    patch = header + "\n" + "\n".join(f"+line {i}" for i in range(300))
    chunks = chunk_file(ChangedFile(filename="x.py", patch=patch), max_chars=1000)
    assert len(chunks) > 1
    assert all(len(c.content) <= 1000 for c in chunks)


async def test_pipeline_run_never_raises_even_if_db_fails(
    settings: Settings, fake_github: FakeGitHub, mock_llm: Any, pr_ctx: PRContext
) -> None:
    state = build_state(settings, llm=mock_llm, github=fake_github.client())

    async def broken(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("database is locked")

    state.repository.set_status = broken  # type: ignore[method-assign]
    assert await state.pipeline.run(1, pr_ctx) is None
