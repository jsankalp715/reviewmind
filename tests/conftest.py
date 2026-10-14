"""Shared fixtures. Nothing here touches the network: GitHub is an ``httpx.MockTransport``
and the LLM is ``MockLLMClient``."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.chunking import parse_unified_diff
from app.config import Settings
from app.github.client import GitHubClient
from app.github.webhook import compute_signature
from app.llm.mock import MockLLMClient
from app.main import create_app
from app.models import ChangedFile, PRContext

FIXTURES = Path(__file__).parent / "fixtures"
WEBHOOK_SECRET = "test-webhook-secret"


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
def sample_diff() -> str:
    return (FIXTURES / "sample_pr.diff").read_text()


@pytest.fixture
def sample_files(sample_diff: str) -> list[ChangedFile]:
    return parse_unified_diff(sample_diff)


@pytest.fixture
def pr_payload() -> dict[str, Any]:
    return json.loads((FIXTURES / "pull_request_opened.json").read_text())


@pytest.fixture
def pr_ctx(pr_payload: dict[str, Any]) -> PRContext:
    pr = pr_payload["pull_request"]
    return PRContext(
        repo_full_name="acme/shop",
        pr_number=42,
        title=pr["title"],
        head_sha=pr["head"]["sha"],
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        llm_provider="mock",
        github_webhook_secret=WEBHOOK_SECRET,
        github_token="test-token",
        database_path=str(tmp_path / "reviewmind.db"),
        agent_timeout_seconds=0.5,
        agent_retry_backoff_seconds=0,
        github_backoff_base_seconds=0,
        log_format="text",
    )


# --------------------------------------------------------------------------------------------
# Fake GitHub API
# --------------------------------------------------------------------------------------------


@dataclass
class FakeGitHub:
    """An in-memory stand-in for the subset of the GitHub REST API the app uses."""

    files: list[dict[str, Any]]
    head_sha: str
    diff_text: str = ""
    per_page: int = 2  # small, so pagination is always exercised
    requests: list[httpx.Request] = field(default_factory=list)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    comments: list[dict[str, Any]] = field(default_factory=list)
    fail_review_post: int | None = None  # HTTP status to return when a review is posted
    overrides: dict[str, Callable[[httpx.Request], httpx.Response]] = field(default_factory=dict)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        for pattern, override in self.overrides.items():
            if re.fullmatch(pattern, path):
                return override(request)
        if m := re.fullmatch(r"/repos/([^/]+)/([^/]+)/pulls/(\d+)/files", path):
            page = int(request.url.params.get("page", "1"))
            per_page = self.per_page
            start = (page - 1) * per_page
            chunk = self.files[start : start + per_page]
            headers = {}
            if start + per_page < len(self.files):
                nxt = request.url.copy_set_param("page", str(page + 1))
                headers["Link"] = f'<{nxt}>; rel="next"'
            return httpx.Response(200, json=chunk, headers=headers)
        if m := re.fullmatch(r"/repos/([^/]+)/([^/]+)/pulls/(\d+)/reviews", path):
            if self.fail_review_post:
                return httpx.Response(self.fail_review_post, json={"message": "nope"})
            body = json.loads(request.content)
            self.reviews.append(body)
            n = len(self.reviews)
            return httpx.Response(
                200,
                json={"id": n, "html_url": f"https://github.com/{m[1]}/{m[2]}/pull/{m[3]}#r{n}"},
            )
        if m := re.fullmatch(r"/repos/([^/]+)/([^/]+)/issues/(\d+)/comments", path):
            body = json.loads(request.content)
            self.comments.append(body)
            return httpx.Response(
                201, json={"html_url": f"https://github.com/{m[1]}/{m[2]}/pull/{m[3]}#c1"}
            )
        if re.fullmatch(r"/repos/([^/]+)/([^/]+)/pulls/(\d+)", path):
            if "diff" in request.headers.get("accept", ""):
                return httpx.Response(200, text=self.diff_text)
            return httpx.Response(200, json={"number": 42, "head": {"sha": self.head_sha}})
        return httpx.Response(404, json={"message": f"unexpected {request.method} {path}"})

    def client(self) -> GitHubClient:
        return GitHubClient(
            "test-token",
            transport=httpx.MockTransport(self.handler),
            backoff_base_seconds=0,
            sleep=_no_sleep,
        )


def files_api_json(files: list[ChangedFile]) -> list[dict[str, Any]]:
    out = []
    for f in files:
        item = f.model_dump(exclude={"previous_filename"})
        if item["patch"] is None:
            del item["patch"]
        out.append(item)
    return out


@pytest.fixture
def fake_github(sample_files: list[ChangedFile], pr_ctx: PRContext, sample_diff: str) -> FakeGitHub:
    return FakeGitHub(
        files=files_api_json(sample_files), head_sha=pr_ctx.head_sha, diff_text=sample_diff
    )


@pytest.fixture
def mock_llm() -> MockLLMClient:
    return MockLLMClient()


@pytest.fixture
def make_client(
    settings: Settings, fake_github: FakeGitHub, mock_llm: MockLLMClient
) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def _make(**overrides: Any) -> TestClient:
        # Re-validate (model_copy would skip validation, e.g. leave secrets as plain str).
        app_settings = Settings(_env_file=None, **{**settings.model_dump(), **overrides})
        app = create_app(app_settings, llm=mock_llm, github=fake_github.client())
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()


def signed_headers(
    body: bytes,
    *,
    event: str = "pull_request",
    delivery: str = "delivery-1",
    secret: str = WEBHOOK_SECRET,
) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": compute_signature(secret, body),
    }
