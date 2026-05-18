import logging
import sys
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request

from config import APP_VERSION
from database import get_review_detail, get_review_history, init_db
from models import HealthResponse
from orchestrator import run_review
from webhook import parse_pr_context, verify_signature

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("ReviewMind %s started", APP_VERSION)
    yield


app = FastAPI(title="ReviewMind", version=APP_VERSION, lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(status="ok", version=APP_VERSION)


@app.post("/webhook", status_code=200)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    body: bytes = Depends(verify_signature),
):
    import json

    event_type = request.headers.get("X-GitHub-Event", "")
    if event_type != "pull_request":
        logger.debug("Ignoring event type: %s", event_type)
        return {"message": "ignored"}

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    ctx = parse_pr_context(payload)
    if ctx is None:
        return {"message": "ignored"}

    background_tasks.add_task(
        run_review,
        pr_number=ctx.pr_number,
        repo_full_name=ctx.repo_full_name,
        pr_title=ctx.pr_title,
        base_branch=ctx.base_branch,
        head_branch=ctx.head_branch,
        diff=ctx.diff,
    )
    logger.info("Queued review for %s#%d", ctx.repo_full_name, ctx.pr_number)
    return {"message": "review queued", "pr": ctx.pr_number}


@app.get("/reviews/{repo_owner}/{repo_name}")
async def list_reviews(repo_owner: str, repo_name: str):
    repo_full = f"{repo_owner}/{repo_name}"
    history = get_review_history(repo_full)
    return {"repo": repo_full, "reviews": history}


@app.get("/reviews/{repo_owner}/{repo_name}/{pr_number}")
async def get_review(repo_owner: str, repo_name: str, pr_number: int):
    repo_full = f"{repo_owner}/{repo_name}"
    detail = get_review_detail(repo_full, pr_number)
    if not detail:
        raise HTTPException(status_code=404, detail="Review not found")
    return detail
