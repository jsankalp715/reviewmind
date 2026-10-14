"""Turn LLM output into a validated ``LLMReviewResponse``.

Models asked for "JSON only" still sometimes return:
- JSON wrapped in ```json ... ``` code fences,
- JSON with a sentence of prose before or after it,
- trailing commas before ``}`` or ``]``,
- a bare list of findings instead of ``{"findings": [...]}``.

``parse_review_response`` repairs these cases. Anything else raises ``InvalidLLMOutput``, which
the agent treats as a failed attempt (it retries once, then degrades).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import ValidationError

from app.models import Finding, LLMReviewResponse

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)\n?\s*```", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


class InvalidLLMOutput(ValueError):
    """The model's response could not be repaired into the expected schema."""


def strip_code_fences(text: str) -> str:
    """Return the contents of the first fenced block, or ``text`` unchanged if there is none."""
    match = _FENCE_RE.search(text)
    return match.group(1).strip() if match else text.strip()


def _first_balanced_json(text: str) -> str | None:
    """Find the first balanced ``{...}`` or ``[...]`` span, respecting strings and escapes."""
    for start, char in enumerate(text):
        if char not in "{[":
            continue
        stack: list[str] = []
        in_string = escaped = False
        for end in range(start, len(text)):
            c = text[end]
            if in_string:
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    in_string = False
                continue
            if c == '"':
                in_string = True
            elif c in "{[":
                stack.append("}" if c == "{" else "]")
            elif c in "}]":
                if not stack or stack.pop() != c:
                    break
                if not stack:
                    return text[start : end + 1]
        # Unbalanced from this start; try the next opening bracket.
    return None


def extract_json(text: str) -> Any:
    """Best-effort extraction of a JSON value from model output."""
    if not text or not text.strip():
        raise InvalidLLMOutput("empty response")
    candidates = [strip_code_fences(text)]
    if (span := _first_balanced_json(candidates[0])) is not None:
        candidates.append(span)
    for candidate in candidates:
        for attempt in (candidate, _TRAILING_COMMA_RE.sub(r"\1", candidate)):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue
    raise InvalidLLMOutput(f"no valid JSON found in response: {text[:120]!r}")


def parse_review_response(text: str) -> LLMReviewResponse:
    """Parse and validate model output into findings.

    The top-level shape must be valid. Individual findings that fail validation are dropped
    and logged instead of discarding the whole response. If *every* finding is invalid, the
    response is rejected so the agent retries.
    """
    data = extract_json(text)
    if isinstance(data, list):
        data = {"findings": data}
    if not isinstance(data, dict):
        raise InvalidLLMOutput(f"expected a JSON object, got {type(data).__name__}")
    raw_findings = data.get("findings", [])
    if raw_findings is None:
        raw_findings = []
    if not isinstance(raw_findings, list):
        raise InvalidLLMOutput("'findings' must be a list")

    findings: list[Finding] = []
    errors: list[str] = []
    for item in raw_findings:
        try:
            findings.append(Finding.model_validate(item))
        except ValidationError as exc:
            errors.append(exc.errors()[0].get("msg", str(exc)) if exc.errors() else str(exc))
    if raw_findings and not findings:
        raise InvalidLLMOutput(f"all {len(raw_findings)} findings failed validation: {errors[0]}")
    if errors:
        logger.warning("dropped %d invalid finding(s)", len(errors), extra={"errors": errors[:3]})

    summary = data.get("summary", "")
    return LLMReviewResponse(findings=findings, summary=summary if isinstance(summary, str) else "")
