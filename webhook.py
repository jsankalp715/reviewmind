import hashlib
import hmac
import logging

from fastapi import Header, HTTPException, Request

from config import GITHUB_WEBHOOK_SECRET
from github_client import get_pr_diff
from models import PRContext

logger = logging.getLogger(__name__)


async def verify_signature(request: Request, x_hub_signature_256: str | None = Header(default=None)) -> bytes:
    body = await request.body()
    if not GITHUB_WEBHOOK_SECRET:
        logger.warning("GITHUB_WEBHOOK_SECRET not set — skipping signature verification")
        return body

    if not x_hub_signature_256:
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256 header")

    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    return body


def parse_pr_context(payload: dict) -> PRContext | None:
    action = payload.get("action")
    if action not in ("opened", "synchronize"):
        logger.debug("Ignoring PR action: %s", action)
        return None

    event = payload.get("pull_request") or {}
    repo = payload.get("repository") or {}

    pr_number = event.get("number") or payload.get("number")
    repo_full_name = repo.get("full_name", "")
    pr_title = event.get("title", "")
    base_branch = (event.get("base") or {}).get("ref", "")
    head_branch = (event.get("head") or {}).get("ref", "")

    if not pr_number or not repo_full_name:
        logger.warning("Incomplete PR payload — skipping")
        return None

    try:
        diff = get_pr_diff(repo_full_name, pr_number)
    except Exception as exc:
        logger.error("Could not fetch diff for %s#%d: %s", repo_full_name, pr_number, exc)
        return None

    return PRContext(
        pr_number=pr_number,
        repo_full_name=repo_full_name,
        pr_title=pr_title,
        base_branch=base_branch,
        head_branch=head_branch,
        diff=diff,
    )
