from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class Finding(BaseModel):
    line: str
    issue: str
    suggestion: str
    severity: str = Field(pattern="^(low|medium|high)$")


class AgentResult(BaseModel):
    agent: str
    findings: list[Finding]
    severity: str
    summary: str


class PRContext(BaseModel):
    pr_number: int
    repo_full_name: str
    pr_title: str
    base_branch: str
    head_branch: str
    diff: str


class WebhookPayload(BaseModel):
    action: str
    number: Optional[int] = None
    pull_request: Optional[dict] = None
    repository: Optional[dict] = None


class ReviewRecord(BaseModel):
    id: Optional[int] = None
    repo: str
    pr_number: int
    pr_title: str
    timestamp: datetime
    total_findings: int
    comment_url: Optional[str] = None


class FindingRecord(BaseModel):
    id: Optional[int] = None
    review_id: int
    agent: str
    severity: str
    issue: str
    suggestion: str


class HealthResponse(BaseModel):
    status: str
    version: str


class ReviewSummary(BaseModel):
    id: int
    repo: str
    pr_number: int
    pr_title: str
    timestamp: str
    total_findings: int
    comment_url: Optional[str]
