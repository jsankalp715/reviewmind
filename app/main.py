"""FastAPI application: the webhook receiver and the read API."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import cache
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.agents import build_agents
from app.config import APP_VERSION, Settings, get_settings
from app.db import ReviewRepository
from app.github.client import GitHubClient
from app.github.webhook import (
    HANDLED_ACTIONS,
    HANDLED_EVENT,
    PayloadError,
    parse_pull_request_event,
    verify_signature,
)
from app.llm import LLMClient, build_llm_client
from app.logging_config import bind_log_context, configure_logging
from app.orchestrator import AgentOrchestrator
from app.pipeline import ReviewPipeline

logger = logging.getLogger(__name__)


@dataclass
class AppState:
    settings: Settings
    repository: ReviewRepository
    github: GitHubClient
    llm: LLMClient
    pipeline: ReviewPipeline


def build_state(
    settings: Settings, *, llm: LLMClient | None = None, github: GitHubClient | None = None
) -> AppState:
    """Wire up the dependency graph. Tests inject a mock LLM and a mock-transport GitHub client."""
    llm = llm or build_llm_client(settings)
    github = github or GitHubClient(
        settings.github_token.get_secret_value() if settings.github_token else None,
        base_url=settings.github_api_url,
        timeout_seconds=settings.github_timeout_seconds,
        max_retries=settings.github_max_retries,
        backoff_base_seconds=settings.github_backoff_base_seconds,
        backoff_max_seconds=settings.github_backoff_max_seconds,
    )
    repository = ReviewRepository(settings.database_path)
    # One semaphore shared by every agent and every concurrent review bounds total LLM calls.
    semaphore = asyncio.Semaphore(settings.llm_max_concurrency)
    orchestrator = AgentOrchestrator(build_agents(llm, settings, semaphore))
    pipeline = ReviewPipeline(settings, github, orchestrator, repository)
    return AppState(settings, repository, github, llm, pipeline)


def create_app(
    settings: Settings | None = None,
    *,
    llm: LLMClient | None = None,
    github: GitHubClient | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state = build_state(settings, llm=llm, github=github)
        await state.repository.init()
        app.state.rm = state
        if not settings.webhook_secret:
            logger.error("GITHUB_WEBHOOK_SECRET is not set: every webhook will be rejected")
        logger.info(
            "ReviewMind started",
            extra={"version": APP_VERSION, "llm_provider": settings.llm_provider,
                   "agents": settings.enabled_agents},
        )  # fmt: skip
        try:
            yield
        finally:
            await state.github.aclose()
            await state.llm.aclose()

    app = FastAPI(title="ReviewMind", version=APP_VERSION, lifespan=lifespan)

    def state(request: Request) -> AppState:
        rm: AppState = request.app.state.rm
        return rm

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        st = state(request)
        db_ok = await st.repository.ping()
        body = {
            "status": "ok" if db_ok else "degraded",
            "version": APP_VERSION,
            "llm_provider": st.settings.llm_provider,
            "database": "ok" if db_ok else "unavailable",
            "webhook_secret_configured": bool(st.settings.webhook_secret),
        }
        return JSONResponse(body, status_code=200 if db_ok else 503)

    async def github_webhook(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
        st = state(request)
        secret = st.settings.webhook_secret
        if not secret:
            # Fail closed: without a secret we cannot authenticate the sender.
            raise HTTPException(status_code=503, detail="webhook secret not configured")

        body = await request.body()  # raw bytes, exactly as signed by GitHub
        if not verify_signature(secret, body, request.headers.get("X-Hub-Signature-256")):
            raise HTTPException(status_code=401, detail="invalid or missing signature")

        event = request.headers.get("X-GitHub-Event", "")
        delivery_id = request.headers.get("X-GitHub-Delivery", "")
        bind_log_context(delivery_id=delivery_id or None, gh_event=event)
        if event == "ping":
            return JSONResponse({"status": "pong"}, status_code=200)
        if event != HANDLED_EVENT:
            return JSONResponse({"status": "ignored", "reason": f"event {event!r}"}, 200)

        try:
            payload: Any = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        action = payload.get("action")
        if action not in HANDLED_ACTIONS:
            return JSONResponse({"status": "ignored", "reason": f"action {action!r}"}, 200)
        try:
            ctx = parse_pull_request_event(payload)
        except PayloadError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not delivery_id:
            raise HTTPException(status_code=400, detail="missing X-GitHub-Delivery header")

        claim = await st.repository.claim_delivery(delivery_id, ctx, str(action))
        if not claim.is_new:
            logger.info("duplicate delivery ignored", extra={"review_id": claim.review_id})
            return JSONResponse(
                {
                    "status": "duplicate",
                    "review_id": claim.review_id,
                    "review_status": claim.status,
                },
                status_code=200,
            )

        background_tasks.add_task(st.pipeline.run, claim.review_id, ctx)
        logger.info("review queued", extra={"review_id": claim.review_id, "pr": ctx.pr_number})
        return JSONResponse({"status": "queued", "review_id": claim.review_id}, status_code=202)

    app.add_api_route("/webhook/github", github_webhook, methods=["POST"], status_code=202)
    # Backwards-compatible path used by the original version of this app.
    app.add_api_route(
        "/webhook", github_webhook, methods=["POST"], status_code=202, include_in_schema=False
    )

    @app.get("/reviews")
    async def list_reviews(
        request: Request,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        repo: str | None = Query(None, description="owner/name"),
        pr_number: int | None = Query(None, ge=1),
    ) -> dict[str, Any]:
        reviews = await state(request).repository.list_reviews(
            limit=limit, offset=offset, repo=repo, pr_number=pr_number
        )
        return {"reviews": reviews, "limit": limit, "offset": offset}

    @app.get("/reviews/{review_id}")
    async def get_review(request: Request, review_id: int) -> dict[str, Any]:
        review = await state(request).repository.get_review(review_id)
        if review is None:
            raise HTTPException(status_code=404, detail="review not found")
        return review

    return app


@cache
def _app_from_env() -> FastAPI:
    return create_app()


# ``uvicorn app.main:app`` entry point. Built lazily via module __getattr__ so that importing
# this module in tests does not require production settings.
def __getattr__(name: str) -> Any:
    if name == "app":
        return _app_from_env()
    raise AttributeError(name)
