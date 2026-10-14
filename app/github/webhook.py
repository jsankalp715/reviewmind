"""GitHub webhook security and payload parsing (pure functions, so they are easy to test)."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from app.models import PRContext

SIGNATURE_PREFIX = "sha256="
HANDLED_EVENT = "pull_request"
HANDLED_ACTIONS = frozenset({"opened", "synchronize", "reopened"})


def compute_signature(secret: str, body: bytes) -> str:
    """The ``X-Hub-Signature-256`` value GitHub sends for ``body`` signed with ``secret``."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return SIGNATURE_PREFIX + digest


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Check a webhook signature in constant time.

    ``body`` must be the *raw* request bytes. Re-serialising parsed JSON changes whitespace and
    key order, so the HMAC would never match (and verifying parsed data defeats the purpose).
    """
    if not secret or not signature_header or not signature_header.startswith(SIGNATURE_PREFIX):
        return False
    expected = compute_signature(secret, body)
    return hmac.compare_digest(expected.encode("ascii"), signature_header.strip().encode("utf-8"))


class PayloadError(ValueError):
    """A signed ``pull_request`` payload that is missing required fields."""


def parse_pull_request_event(payload: dict[str, Any]) -> PRContext:
    """Extract the PR context from a ``pull_request`` event payload."""
    pr = payload.get("pull_request")
    repo = payload.get("repository")
    if not isinstance(pr, dict) or not isinstance(repo, dict):
        raise PayloadError("payload is missing 'pull_request' or 'repository'")
    number = pr.get("number", payload.get("number"))
    full_name = repo.get("full_name")
    if not isinstance(number, int) or not isinstance(full_name, str) or "/" not in full_name:
        raise PayloadError("payload is missing a PR number or repository full_name")
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    return PRContext(
        repo_full_name=full_name,
        pr_number=number,
        title=str(pr.get("title") or ""),
        head_sha=str(head.get("sha") or ""),
        head_ref=str(head.get("ref") or ""),
        base_ref=str(base.get("ref") or ""),
        author=str((pr.get("user") or {}).get("login") or ""),
    )
