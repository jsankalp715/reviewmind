"""Async GitHub REST client (httpx) with pagination and retry/backoff."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.config import APP_VERSION
from app.models import ChangedFile

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
MAX_FILES_PAGES = 30  # GitHub returns at most 3,000 files per PR (30 pages of 100)


class GitHubAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _is_rate_limited(resp: httpx.Response) -> bool:
    """Primary (remaining=0) or secondary rate limit. GitHub signals both with 403 or 429."""
    if resp.status_code == 429:
        return True
    if resp.status_code != 403:
        return False
    if resp.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in resp.headers:
        return True
    return "rate limit" in resp.text.lower()


class GitHubClient:
    """A minimal client for the endpoints ReviewMind needs.

    Retry policy:
    - Idempotent requests (GET) are retried on 429, 5xx, rate-limit 403s, and transport errors.
    - Non-idempotent requests (POST a review) are retried only when GitHub definitely did not
      process them (a rate-limit response or a connection failure). Retrying a POST after a
      502 or a read timeout could post the review twice.
    - The delay honours ``Retry-After`` and ``X-RateLimit-Reset``; otherwise it is exponential
      backoff with jitter. If GitHub asks for a longer wait than ``backoff_max``, we fail fast
      instead of holding the task open for up to an hour.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = "https://api.github.com",
        timeout_seconds: float = 30.0,
        max_retries: int = 4,
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"ReviewMind/{APP_VERSION}",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout_seconds,
            transport=transport,
        )
        self.max_retries = max_retries
        self.backoff_base = backoff_base_seconds
        self.backoff_max = backoff_max_seconds
        self._sleep = sleep

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------------------------------------
    # Core request loop
    # ---------------------------------------------------------------------------------------

    def _retry_delay(self, attempt: int, resp: httpx.Response | None) -> float:
        if resp is not None:
            if (retry_after := resp.headers.get("retry-after")) is not None:
                try:
                    return max(float(retry_after), 0.0)
                except ValueError:
                    pass
            if resp.headers.get("x-ratelimit-remaining") == "0" and (
                reset := resp.headers.get("x-ratelimit-reset")
            ):
                try:
                    return max(float(reset) - time.time(), 0.0) + 1.0
                except ValueError:
                    pass
        exponential = self.backoff_base * (2**attempt)
        return exponential + random.uniform(0, self.backoff_base)

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Send a request with retries. Raises ``GitHubAPIError`` on a final non-2xx."""
        method = method.upper()
        idempotent = method in IDEMPOTENT_METHODS
        attempt = 0
        while True:
            resp: httpx.Response | None = None
            try:
                resp = await self._http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                # A connect error means the request never reached GitHub, so even a POST is safe.
                safe = idempotent or isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)
                if not safe or attempt >= self.max_retries:
                    raise GitHubAPIError(f"{method} {url} failed: {type(exc).__name__}") from exc
                reason = type(exc).__name__
            else:
                if resp.is_success:
                    return resp
                rate_limited = _is_rate_limited(resp)
                retryable = rate_limited or (idempotent and resp.status_code in RETRYABLE_STATUS)
                if not retryable or attempt >= self.max_retries:
                    raise GitHubAPIError(
                        f"{method} {url} -> HTTP {resp.status_code}: {_message(resp)}",
                        status_code=resp.status_code,
                    )
                reason = f"HTTP {resp.status_code}" + (" (rate limited)" if rate_limited else "")

            delay = self._retry_delay(attempt, resp)
            if delay > self.backoff_max:
                raise GitHubAPIError(
                    f"{method} {url}: GitHub asked us to wait {delay:.0f}s "
                    f"(limit {self.backoff_max:.0f}s); giving up",
                    status_code=resp.status_code if resp is not None else None,
                )
            attempt += 1
            logger.warning(
                "github request retry",
                extra={"method": method, "url": url, "reason": reason, "attempt": attempt,
                       "delay_s": round(delay, 2)},
            )  # fmt: skip
            await self._sleep(delay)

    async def paginate(self, url: str, *, params: dict[str, Any] | None = None,
                       max_pages: int = MAX_FILES_PAGES) -> list[Any]:  # fmt: skip
        """Follow ``Link: rel="next"`` headers and concatenate the JSON arrays."""
        items: list[Any] = []
        next_url: str | None = url
        next_params = {"per_page": 100, **(params or {})}
        pages = 0
        while next_url and pages < max_pages:
            resp = await self.request("GET", next_url, params=next_params)
            page = resp.json()
            if not isinstance(page, list):
                raise GitHubAPIError(f"expected a JSON array from {next_url}")
            items.extend(page)
            pages += 1
            next_link = resp.links.get("next")
            next_url = next_link.get("url") if next_link else None
            next_params = None  # the "next" URL already carries the query string
        if next_url:
            logger.warning("pagination stopped at page limit", extra={"url": url, "pages": pages})
        return items

    # ---------------------------------------------------------------------------------------
    # Endpoints
    # ---------------------------------------------------------------------------------------

    async def get_pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        resp = await self.request("GET", f"/repos/{owner}/{repo}/pulls/{number}")
        data: dict[str, Any] = resp.json()
        return data

    async def list_pull_request_files(
        self, owner: str, repo: str, number: int
    ) -> list[ChangedFile]:
        raw = await self.paginate(f"/repos/{owner}/{repo}/pulls/{number}/files")
        return [
            ChangedFile(
                filename=f["filename"],
                status=f.get("status", "modified"),
                additions=f.get("additions", 0),
                deletions=f.get("deletions", 0),
                patch=f.get("patch"),
                previous_filename=f.get("previous_filename"),
            )
            for f in raw
        ]

    async def get_pull_request_diff(self, owner: str, repo: str, number: int) -> str:
        """The full unified diff (GitHub refuses this for very large PRs with HTTP 406)."""
        resp = await self.request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{number}",
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
        return resp.text

    async def create_review(
        self, owner: str, repo: str, number: int, body: str, commit_id: str | None = None
    ) -> str:
        payload: dict[str, Any] = {"body": body, "event": "COMMENT"}
        if commit_id:
            payload["commit_id"] = commit_id
        resp = await self.request("POST", f"/repos/{owner}/{repo}/pulls/{number}/reviews",
                                  json=payload)  # fmt: skip
        return str(resp.json().get("html_url", ""))

    async def create_issue_comment(self, owner: str, repo: str, number: int, body: str) -> str:
        resp = await self.request("POST", f"/repos/{owner}/{repo}/issues/{number}/comments",
                                  json={"body": body})  # fmt: skip
        return str(resp.json().get("html_url", ""))

    async def post_review(
        self, owner: str, repo: str, number: int, body: str, commit_id: str | None = None
    ) -> str:
        """Post as a PR review. If GitHub rejects it (422, e.g. a stale ``commit_id``), fall back
        to a plain PR comment so the result is not lost."""
        try:
            return await self.create_review(owner, repo, number, body, commit_id)
        except GitHubAPIError as exc:
            if exc.status_code != 422:
                raise
            logger.warning("review rejected with 422; falling back to an issue comment")
            return await self.create_issue_comment(owner, repo, number, body)


def _message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        return str(data.get("message", ""))[:300] if isinstance(data, dict) else resp.text[:300]
    except ValueError:
        return resp.text[:300]
