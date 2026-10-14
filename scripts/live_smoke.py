#!/usr/bin/env python
"""Smoke-test ReviewMind against the real Gemini and GitHub APIs.

Reads configuration from the environment / .env (GEMINI_API_KEY, GITHUB_TOKEN, ...).
Each mode is a separate, explicit opt-in:

  # 1. Check the Gemini key and model with one tiny call (costs a few tokens)
  python scripts/live_smoke.py gemini

  # 2. Review a real PR end to end and print the Markdown. Nothing is posted.
  python scripts/live_smoke.py review --repo OWNER/REPO --pr 123

  # 3. Same, but also post the review to the PR (needs a token with PR write access)
  python scripts/live_smoke.py review --repo OWNER/REPO --pr 123 --post

  # 4. Send a correctly signed webhook for a real PR to a running server
  #    (e.g. uvicorn locally, or your ngrok/smee URL). Uses GITHUB_WEBHOOK_SECRET.
  python scripts/live_smoke.py webhook --repo OWNER/REPO --pr 123 \
      --url http://localhost:8000/webhook/github

Exit code: 0 on success, 1 if any agent degraded or a call failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents import build_agents
from app.config import Settings
from app.db import ReviewRepository
from app.github.client import GitHubClient
from app.github.webhook import compute_signature
from app.llm import LLMRequest, build_llm_client
from app.llm.json_repair import parse_review_response
from app.logging_config import configure_logging
from app.models import AgentStatus, PRContext
from app.orchestrator import AgentOrchestrator
from app.pipeline import ReviewPipeline


def github_client(settings: Settings) -> GitHubClient:
    token = settings.github_token.get_secret_value() if settings.github_token else None
    if not token:
        print("warning: GITHUB_TOKEN not set; unauthenticated requests (public repos, 60/hour)")
    return GitHubClient(token, base_url=settings.github_api_url)


async def smoke_gemini(settings: Settings) -> int:
    llm = build_llm_client(settings)
    print(f"provider={settings.llm_provider} model={settings.gemini_model}")
    started = time.perf_counter()
    try:
        resp = await llm.complete(
            LLMRequest(
                system='Reply with JSON only: {"findings": [], "summary": "<one word>"}',
                prompt="Say hello.",
                agent="smoke",
            )
        )
    except Exception as exc:
        print(f"FAILED: {exc}")
        if "404" in str(exc):
            print("hint: the model name may be retired; try GEMINI_MODEL=gemini-2.5-flash")
        return 1
    finally:
        await llm.aclose()
    parsed = parse_review_response(resp.text)
    print(
        f"OK in {time.perf_counter() - started:.2f}s, tokens={resp.total_tokens}, "
        f"summary={parsed.summary!r}"
    )
    return 0


async def pr_context(gh: GitHubClient, repo: str, number: int) -> PRContext:
    owner, name = repo.split("/", 1)
    pr = await gh.get_pull_request(owner, name, number)
    return PRContext(
        repo_full_name=repo,
        pr_number=number,
        title=pr.get("title", ""),
        head_sha=pr["head"]["sha"],
        head_ref=pr["head"]["ref"],
        base_ref=pr["base"]["ref"],
        author=(pr.get("user") or {}).get("login", ""),
    )


async def smoke_review(settings: Settings, repo: str, number: int, post: bool) -> int:
    gh = github_client(settings)
    llm = build_llm_client(settings)
    try:
        ctx = await pr_context(gh, repo, number)
        print(f"PR {repo}#{number}: {ctx.title!r} @ {ctx.head_sha[:7]}")
        semaphore = asyncio.Semaphore(settings.llm_max_concurrency)
        pipeline = ReviewPipeline(
            settings,
            gh,
            AgentOrchestrator(build_agents(llm, settings, semaphore)),
            ReviewRepository(settings.database_path),
        )
        files = await pipeline.fetch_files(ctx)
        print(f"{len(files)} changed file(s); running {len(settings.enabled_agents)} agents...")
        started = time.perf_counter()
        result = await pipeline.review(ctx, files)
        print(f"review finished in {time.perf_counter() - started:.1f}s\n")
        for run in result.agent_runs:
            print(
                f"  {run.agent:<13} {run.status.value:<9} findings={len(run.findings):<3} "
                f"{run.latency_ms} ms  attempts={run.attempts}  tokens~{run.token_estimate}"
                + (f"\n      error: {run.error}" if run.error else "")
            )
        print("\n" + result.markdown + "\n")
        if post:
            url = await gh.post_review(
                ctx.owner, ctx.repo, ctx.pr_number, result.markdown, ctx.head_sha
            )
            print(f"posted: {url}")
        degraded = any(r.status is AgentStatus.DEGRADED for r in result.agent_runs)
        return 1 if degraded else 0
    finally:
        await gh.aclose()
        await llm.aclose()


async def smoke_webhook(settings: Settings, repo: str, number: int, url: str) -> int:
    secret = settings.webhook_secret
    if not secret:
        print("GITHUB_WEBHOOK_SECRET must be set (and match the server's)")
        return 1
    gh = github_client(settings)
    try:
        ctx = await pr_context(gh, repo, number)
    finally:
        await gh.aclose()
    payload = {
        "action": "synchronize",
        "number": number,
        "pull_request": {
            "number": number,
            "title": ctx.title,
            "user": {"login": ctx.author},
            "head": {"sha": ctx.head_sha, "ref": ctx.head_ref},
            "base": {"ref": ctx.base_ref},
        },
        "repository": {"full_name": repo},
    }
    body = json.dumps(payload).encode()
    delivery = f"smoke-{uuid.uuid4()}"
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": compute_signature(secret, body),
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, content=body, headers=headers)
        print(f"POST {url} -> {resp.status_code} {resp.text}")
        if resp.status_code != 202:
            return 1
        base = url.split("/webhook")[0]
        review_url = f"{base}/reviews/{resp.json()['review_id']}"
        print(f"polling {review_url} ...")
        for _ in range(120):
            review = (await client.get(review_url)).json()
            if review["status"] not in ("queued", "running"):
                print(
                    json.dumps(
                        {
                            k: review[k]
                            for k in (
                                "status",
                                "total_findings",
                                "degraded_agents",
                                "comment_url",
                                "error",
                            )
                        },
                        indent=2,
                    )
                )
                return 0 if review["status"] == "completed" else 1
            await asyncio.sleep(2)
    print("timed out waiting for the review")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("gemini", help="one small Gemini call to validate key and model")
    for name in ("review", "webhook"):
        p = sub.add_parser(name)
        p.add_argument("--repo", required=True, help="OWNER/REPO")
        p.add_argument("--pr", required=True, type=int)
        if name == "review":
            p.add_argument("--post", action="store_true", help="post the review to the PR")
        else:
            p.add_argument("--url", default="http://localhost:8000/webhook/github")
    args = parser.parse_args()

    settings = Settings()
    configure_logging("WARNING", "text")
    if args.mode == "gemini":
        return asyncio.run(smoke_gemini(settings))
    if args.mode == "review":
        return asyncio.run(smoke_review(settings, args.repo, args.pr, args.post))
    return asyncio.run(smoke_webhook(settings, args.repo, args.pr, args.url))


if __name__ == "__main__":
    raise SystemExit(main())
