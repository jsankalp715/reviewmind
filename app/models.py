"""Domain models shared across the pipeline."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Severity(StrEnum):
    """Finding severity, ordered from most to least serious."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        """0 for the most severe; used for sorting."""
        return _SEVERITY_ORDER.index(self)


_SEVERITY_ORDER = [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO]

# Words LLMs commonly use instead of our canonical severities.
_SEVERITY_ALIASES = {
    "blocker": Severity.CRITICAL,
    "severe": Severity.CRITICAL,
    "error": Severity.HIGH,
    "major": Severity.HIGH,
    "moderate": Severity.MEDIUM,
    "warning": Severity.MEDIUM,
    "warn": Severity.MEDIUM,
    "minor": Severity.LOW,
    "nit": Severity.LOW,
    "trivial": Severity.LOW,
    "note": Severity.INFO,
    "informational": Severity.INFO,
}

_INT_RE = re.compile(r"\d+")


class Finding(BaseModel):
    """One issue reported by an agent. This is the schema the LLM must return per finding."""

    file: str = Field(min_length=1, max_length=500)
    line: int | None = Field(default=None, ge=1)
    severity: Severity
    title: str = Field(min_length=1, max_length=300)
    explanation: str = Field(default="", max_length=4000)
    suggestion: str = Field(default="", max_length=4000)
    agent: str = ""

    @field_validator("file", "title", "explanation", "suggestion", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("severity", mode="before")
    @classmethod
    def _normalise_severity(cls, value: Any) -> Any:
        if isinstance(value, str):
            lowered = value.strip().lower()
            return _SEVERITY_ALIASES.get(lowered, lowered)
        return value

    @field_validator("line", mode="before")
    @classmethod
    def _coerce_line(cls, value: Any) -> Any:
        """Accept ``12``, ``"12"``, ``"12-15"`` or ``"app.py:12"``; take the first number."""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 1 else None
        if isinstance(value, float):
            return int(value) if value >= 1 else None
        if isinstance(value, str):
            match = _INT_RE.search(value.rsplit(":", 1)[-1]) or _INT_RE.search(value)
            if not match:
                return None
            number = int(match.group())
            return number if number >= 1 else None
        return value


class LLMReviewResponse(BaseModel):
    """Top-level JSON object every agent prompt asks the model to return."""

    findings: list[Finding] = Field(default_factory=list)
    summary: str = ""


class AgentStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"


class AgentRun(BaseModel):
    """The outcome of one agent over all chunks of a PR."""

    agent: str
    status: AgentStatus = AgentStatus.OK
    findings: list[Finding] = Field(default_factory=list)
    summary: str = ""
    error: str | None = None
    latency_ms: int = 0
    attempts: int = 0
    chunks_total: int = 0
    chunks_failed: int = 0
    token_estimate: int = 0


class SkippedFile(BaseModel):
    filename: str
    reason: str


class ChangedFile(BaseModel):
    """A file from the GitHub "list pull request files" API."""

    filename: str
    status: str = "modified"
    additions: int = 0
    deletions: int = 0
    patch: str | None = None
    previous_filename: str | None = None


class PRContext(BaseModel):
    """What we need to know about a PR to review it."""

    repo_full_name: str
    pr_number: int
    title: str = ""
    head_sha: str = ""
    base_ref: str = ""
    head_ref: str = ""
    author: str = ""

    @property
    def owner(self) -> str:
        return self.repo_full_name.split("/", 1)[0]

    @property
    def repo(self) -> str:
        return self.repo_full_name.split("/", 1)[1]


class ReviewResult(BaseModel):
    """The aggregated, rendered output of one review."""

    findings: list[Finding]
    agent_runs: list[AgentRun]
    skipped_files: list[SkippedFile]
    files_reviewed: int
    markdown: str

    @property
    def degraded_agents(self) -> list[AgentRun]:
        return [r for r in self.agent_runs if r.status == AgentStatus.DEGRADED]
