"""Event/action filtering, payload validation, and idempotency on X-GitHub-Delivery."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.github.webhook import PayloadError, parse_pull_request_event
from tests.conftest import FakeGitHub, signed_headers


def post(client: TestClient, payload: Any, **kw: Any) -> Any:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return client.post("/webhook/github", content=body, headers=signed_headers(body, **kw))


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened"])
def test_handled_actions_return_202(
    client: TestClient, pr_payload: dict[str, Any], action: str
) -> None:
    pr_payload["action"] = action
    resp = post(client, pr_payload, delivery=f"d-{action}")
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"


@pytest.mark.parametrize("action", ["closed", "edited", "labeled", "assigned", None])
def test_other_actions_are_ignored_with_200(
    client: TestClient, pr_payload: dict[str, Any], fake_github: FakeGitHub, action: str | None
) -> None:
    pr_payload["action"] = action
    resp = post(client, pr_payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert fake_github.requests == []


@pytest.mark.parametrize("event", ["push", "issues", "pull_request_review", "check_run"])
def test_other_events_are_ignored_with_200(
    client: TestClient, pr_payload: dict[str, Any], event: str
) -> None:
    resp = post(client, pr_payload, event=event)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_ping_event(client: TestClient) -> None:
    resp = post(client, {"zen": "Keep it logically awesome."}, event="ping")
    assert resp.status_code == 200
    assert resp.json() == {"status": "pong"}


def test_invalid_json_returns_400(client: TestClient) -> None:
    assert post(client, b"{not json").status_code == 400


def test_non_object_json_returns_400(client: TestClient) -> None:
    assert post(client, [1, 2, 3]).status_code == 400


def test_incomplete_payload_returns_422(client: TestClient) -> None:
    assert post(client, {"action": "opened", "pull_request": {}}).status_code == 422


def test_missing_delivery_header_returns_400(
    client: TestClient, pr_payload: dict[str, Any]
) -> None:
    body = json.dumps(pr_payload).encode()
    headers = signed_headers(body)
    del headers["X-GitHub-Delivery"]
    assert client.post("/webhook/github", content=body, headers=headers).status_code == 400


class TestIdempotency:
    def test_duplicate_delivery_does_not_produce_duplicate_review(
        self, client: TestClient, pr_payload: dict[str, Any], fake_github: FakeGitHub
    ) -> None:
        first = post(client, pr_payload, delivery="same-guid")
        second = post(client, pr_payload, delivery="same-guid")
        assert first.status_code == 202
        assert second.status_code == 200
        assert second.json()["status"] == "duplicate"
        assert second.json()["review_id"] == first.json()["review_id"]
        assert len(fake_github.reviews) == 1
        assert len(client.get("/reviews").json()["reviews"]) == 1

    def test_different_deliveries_are_reviewed_separately(
        self, client: TestClient, pr_payload: dict[str, Any], fake_github: FakeGitHub
    ) -> None:
        post(client, pr_payload, delivery="guid-1")
        pr_payload["action"] = "synchronize"
        post(client, pr_payload, delivery="guid-2")
        assert len(fake_github.reviews) == 2

    def test_concurrent_duplicate_deliveries(
        self, client: TestClient, pr_payload: dict[str, Any], fake_github: FakeGitHub
    ) -> None:
        """GitHub can redeliver while the first request is still in flight."""
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(
                pool.map(lambda _: post(client, pr_payload, delivery="racy-guid"), range(8))
            )
        statuses = sorted(r.status_code for r in responses)
        assert statuses.count(202) == 1
        assert statuses.count(200) == 7
        assert len(fake_github.reviews) == 1

    def test_redelivery_of_a_failed_review_is_retried(
        self, client: TestClient, pr_payload: dict[str, Any], fake_github: FakeGitHub
    ) -> None:
        fake_github.fail_review_post = 500
        first = post(client, pr_payload, delivery="retry-guid")
        review_id = first.json()["review_id"]
        assert client.get(f"/reviews/{review_id}").json()["status"] == "failed"

        fake_github.fail_review_post = None
        again = post(client, pr_payload, delivery="retry-guid")
        assert again.status_code == 202
        assert again.json()["review_id"] == review_id
        review = client.get(f"/reviews/{review_id}").json()
        assert review["status"] == "completed"
        assert review["attempts"] == 2
        assert len(fake_github.reviews) == 1


class TestParsePullRequestEvent:
    def test_extracts_context(self, pr_payload: dict[str, Any]) -> None:
        ctx = parse_pull_request_event(pr_payload)
        assert ctx.repo_full_name == "acme/shop"
        assert (ctx.owner, ctx.repo, ctx.pr_number) == ("acme", "shop", 42)
        assert ctx.head_sha.startswith("3f2c1a9")
        assert ctx.base_ref == "main" and ctx.head_ref == "feature/auth"
        assert ctx.author == "octocat"

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"pull_request": {"number": 1}},
            {"pull_request": {"number": 1}, "repository": {"full_name": "no-slash"}},
            {"pull_request": {"number": "1"}, "repository": {"full_name": "a/b"}},
        ],
    )
    def test_rejects_incomplete_payloads(self, payload: dict[str, Any]) -> None:
        with pytest.raises(PayloadError):
            parse_pull_request_event(payload)
