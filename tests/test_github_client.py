"""Async GitHub client: pagination, retry/backoff policy, and posting reviews."""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import httpx
import pytest

from app.github.client import GitHubAPIError, GitHubClient

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(
    handler: Handler, *, max_retries: int = 3, backoff_max: float = 60.0
) -> tuple[GitHubClient, list[float]]:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = GitHubClient(
        "tok",
        transport=httpx.MockTransport(handler),
        max_retries=max_retries,
        backoff_base_seconds=0.5,
        backoff_max_seconds=backoff_max,
        sleep=fake_sleep,
    )
    return client, sleeps


def sequence(*responses: httpx.Response | Exception) -> tuple[Handler, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    items = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, Exception):
            raise item
        return item

    return handler, seen


async def test_sends_auth_and_api_headers() -> None:
    handler, seen = sequence(httpx.Response(200, json={"head": {"sha": "abc"}}))
    client, _ = make_client(handler)
    await client.get_pull_request("o", "r", 1)
    headers = seen[0].headers
    assert headers["authorization"] == "Bearer tok"
    assert headers["accept"] == "application/vnd.github+json"
    assert headers["x-github-api-version"] == "2022-11-28"
    assert headers["user-agent"].startswith("ReviewMind/")


async def test_no_token_means_no_auth_header() -> None:
    handler, seen = sequence(httpx.Response(200, json={}))
    client = GitHubClient(None, transport=httpx.MockTransport(handler))
    await client.get_pull_request("o", "r", 1)
    assert "authorization" not in seen[0].headers


async def test_paginates_files_following_link_header() -> None:
    pages = {
        1: [
            {"filename": f"f{i}.py", "status": "added", "patch": "@@ -0,0 +1 @@\n+x"}
            for i in range(100)
        ],
        2: [{"filename": "last.png", "status": "added"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        assert request.url.params["per_page"] == "100"
        headers = {}
        if page == 1:
            headers["Link"] = (
                f'<{request.url.copy_set_param("page", "2")}>; rel="next", '
                f'<{request.url.copy_set_param("page", "2")}>; rel="last"'
            )
        return httpx.Response(200, json=pages[page], headers=headers)

    client, _ = make_client(handler)
    files = await client.list_pull_request_files("o", "r", 7)
    assert len(files) == 101
    assert files[0].patch and files[-1].filename == "last.png" and files[-1].patch is None


async def test_pagination_stops_at_page_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        nxt = request.url.copy_set_param("page", str(page + 1))
        return httpx.Response(200, json=[page], headers={"Link": f'<{nxt}>; rel="next"'})

    client, _ = make_client(handler)
    assert await client.paginate("/x", max_pages=3) == [1, 2, 3]


async def test_paginate_rejects_non_list() -> None:
    handler, _ = sequence(httpx.Response(200, json={"not": "a list"}))
    client, _ = make_client(handler)
    with pytest.raises(GitHubAPIError):
        await client.paginate("/x")


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429])
async def test_get_retries_transient_errors_with_backoff(status: int) -> None:
    handler, seen = sequence(
        httpx.Response(status), httpx.Response(status), httpx.Response(200, json={"ok": True})
    )
    client, sleeps = make_client(handler)
    resp = await client.request("GET", "/thing")
    assert resp.json() == {"ok": True}
    assert len(seen) == 3
    # Exponential: base * 2**attempt plus up to `base` of jitter.
    assert 0.5 <= sleeps[0] <= 1.0 and 1.0 <= sleeps[1] <= 1.5


async def test_gives_up_after_max_retries() -> None:
    handler, seen = sequence(httpx.Response(503, json={"message": "unavailable"}))
    client, sleeps = make_client(handler, max_retries=2)
    with pytest.raises(GitHubAPIError) as info:
        await client.request("GET", "/thing")
    assert info.value.status_code == 503
    assert "unavailable" in str(info.value)
    assert len(seen) == 3 and len(sleeps) == 2


@pytest.mark.parametrize("status", [400, 401, 404, 422])
async def test_client_errors_are_not_retried(status: int) -> None:
    handler, seen = sequence(httpx.Response(status, json={"message": "no"}))
    client, _ = make_client(handler)
    with pytest.raises(GitHubAPIError) as info:
        await client.request("GET", "/thing")
    assert info.value.status_code == status and len(seen) == 1


async def test_primary_rate_limit_waits_until_reset() -> None:
    reset = int(time.time()) + 10
    limited = httpx.Response(
        403,
        json={"message": "API rate limit exceeded"},
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)},
    )
    handler, _ = sequence(limited, httpx.Response(200, json={}))
    client, sleeps = make_client(handler)
    await client.request("GET", "/thing")
    assert 8 <= sleeps[0] <= 12


async def test_secondary_rate_limit_honours_retry_after() -> None:
    limited = httpx.Response(
        403,
        json={"message": "You have exceeded a secondary rate limit"},
        headers={"retry-after": "7"},
    )
    handler, _ = sequence(limited, httpx.Response(200, json={}))
    client, sleeps = make_client(handler)
    await client.request("GET", "/thing")
    assert sleeps == [7.0]


async def test_rate_limit_body_text_without_headers_is_detected() -> None:
    limited = httpx.Response(403, json={"message": "secondary rate limit hit"})
    handler, seen = sequence(limited, httpx.Response(200, json={}))
    client, _ = make_client(handler)
    await client.request("GET", "/thing")
    assert len(seen) == 2


async def test_plain_403_is_not_retried() -> None:
    handler, seen = sequence(httpx.Response(403, json={"message": "Resource not accessible"}))
    client, _ = make_client(handler)
    with pytest.raises(GitHubAPIError):
        await client.request("GET", "/thing")
    assert len(seen) == 1


async def test_fails_fast_when_required_wait_exceeds_limit() -> None:
    limited = httpx.Response(429, headers={"retry-after": "3600"})
    handler, seen = sequence(limited)
    client, sleeps = make_client(handler, backoff_max=60)
    with pytest.raises(GitHubAPIError, match="wait 3600s"):
        await client.request("GET", "/thing")
    assert sleeps == [] and len(seen) == 1


async def test_invalid_retry_after_falls_back_to_backoff() -> None:
    handler, _ = sequence(
        httpx.Response(429, headers={"retry-after": "soon"}), httpx.Response(200, json={})
    )
    client, sleeps = make_client(handler)
    await client.request("GET", "/thing")
    assert 0.5 <= sleeps[0] <= 1.0


async def test_post_is_not_retried_on_5xx() -> None:
    """A 502 on POST may have been processed; retrying could post the review twice."""
    handler, seen = sequence(httpx.Response(502), httpx.Response(200, json={"html_url": "u"}))
    client, _ = make_client(handler)
    with pytest.raises(GitHubAPIError):
        await client.create_review("o", "r", 1, "body")
    assert len(seen) == 1


async def test_post_is_retried_on_rate_limit() -> None:
    handler, seen = sequence(
        httpx.Response(429, headers={"retry-after": "1"}),
        httpx.Response(200, json={"html_url": "u"}),
    )
    client, _ = make_client(handler)
    assert await client.create_review("o", "r", 1, "body") == "u"
    assert len(seen) == 2


async def test_post_retried_on_connect_error_but_not_read_timeout() -> None:
    handler, seen = sequence(
        httpx.ConnectError("refused"), httpx.Response(200, json={"html_url": "u"})
    )
    client, _ = make_client(handler)
    assert await client.create_review("o", "r", 1, "body") == "u"
    assert len(seen) == 2

    handler, seen = sequence(httpx.ReadTimeout("slow"))
    client, _ = make_client(handler)
    with pytest.raises(GitHubAPIError, match="ReadTimeout"):
        await client.create_review("o", "r", 1, "body")
    assert len(seen) == 1


async def test_get_retried_on_transport_errors() -> None:
    handler, seen = sequence(httpx.ReadTimeout("slow"), httpx.Response(200, json={}))
    client, _ = make_client(handler)
    await client.request("GET", "/thing")
    assert len(seen) == 2

    handler, seen = sequence(httpx.ReadTimeout("slow"))
    client, _ = make_client(handler, max_retries=1)
    with pytest.raises(GitHubAPIError):
        await client.request("GET", "/thing")
    assert len(seen) == 2


async def test_create_review_payload() -> None:
    handler, seen = sequence(httpx.Response(200, json={"html_url": "https://gh/r/1"}))
    client, _ = make_client(handler)
    url = await client.create_review("acme", "shop", 42, "## hi", commit_id="abc")
    assert url == "https://gh/r/1"
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/repos/acme/shop/pulls/42/reviews"
    assert json.loads(seen[0].content) == {"body": "## hi", "event": "COMMENT", "commit_id": "abc"}


async def test_post_review_falls_back_to_issue_comment_on_422() -> None:
    handler, seen = sequence(
        httpx.Response(422, json={"message": "commit_id is not part of the pull request"}),
        httpx.Response(201, json={"html_url": "https://gh/c/1"}),
    )
    client, _ = make_client(handler)
    assert await client.post_review("acme", "shop", 42, "body", "stale") == "https://gh/c/1"
    assert seen[1].url.path == "/repos/acme/shop/issues/42/comments"


async def test_post_review_other_errors_propagate() -> None:
    handler, _ = sequence(httpx.Response(401, json={"message": "Bad credentials"}))
    client, _ = make_client(handler)
    with pytest.raises(GitHubAPIError, match="Bad credentials"):
        await client.post_review("acme", "shop", 42, "body")


async def test_get_diff_uses_diff_media_type() -> None:
    handler, seen = sequence(httpx.Response(200, text="diff --git a/x b/x\n"))
    client, _ = make_client(handler)
    assert (await client.get_pull_request_diff("o", "r", 3)).startswith("diff --git")
    assert seen[0].headers["accept"] == "application/vnd.github.v3.diff"


async def test_non_json_error_body() -> None:
    handler, _ = sequence(httpx.Response(418, text="<html>teapot</html>"))
    client, _ = make_client(handler)
    with pytest.raises(GitHubAPIError, match="teapot"):
        await client.request("GET", "/x")
    await client.aclose()
