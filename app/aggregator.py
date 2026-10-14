"""Merge agent output into one review: dedupe, sort by severity, and render Markdown."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher

from app.agents import AGENT_REGISTRY
from app.config import APP_VERSION
from app.models import AgentRun, AgentStatus, Finding, ReviewResult, Severity, SkippedFile

# GitHub rejects review bodies over 65,536 characters; leave headroom for the footer.
MAX_BODY_CHARS = 60_000
TITLE_SIMILARITY = 0.82

SEVERITY_ICON = {
    Severity.CRITICAL: "🔴",
    Severity.HIGH: "🟠",
    Severity.MEDIUM: "🟡",
    Severity.LOW: "🔵",
    Severity.INFO: "⚪",
}

_NORMALISE_RE = re.compile(r"[^a-z0-9]+")
_MENTION_RE = re.compile(r"@(?=[A-Za-z0-9-])")


@dataclass
class MergedFinding:
    finding: Finding
    agents: list[str] = field(default_factory=list)  # first entry is the reporting agent


def _norm(title: str) -> str:
    return _NORMALISE_RE.sub(" ", title.lower()).strip()


def _same_issue(a: Finding, b: Finding) -> bool:
    if a.file != b.file or a.line != b.line:
        return False
    ta, tb = _norm(a.title), _norm(b.title)
    return ta == tb or SequenceMatcher(None, ta, tb).ratio() >= TITLE_SIMILARITY


def sort_key(f: Finding) -> tuple[int, str, int, str]:
    return (f.severity.rank, f.file, f.line or 0, f.title)


def merge_findings(runs: list[AgentRun]) -> list[MergedFinding]:
    """Flatten every agent's findings, collapse duplicates, and sort by severity.

    Two findings are duplicates when they are on the same file and line and their titles match
    exactly or are near-identical. Duplicates from different agents collapse into one entry that
    keeps the highest severity and remembers which agents reported it.
    """
    merged: list[MergedFinding] = []
    # Only findings on the same (file, line) can be duplicates, so bucket by location.
    buckets: dict[tuple[str, int | None], list[MergedFinding]] = {}
    for run in runs:
        for finding in run.findings:
            bucket = buckets.setdefault((finding.file, finding.line), [])
            existing = next((m for m in bucket if _same_issue(m.finding, finding)), None)
            if existing is None:
                entry = MergedFinding(finding=finding, agents=[finding.agent or run.agent])
                bucket.append(entry)
                merged.append(entry)
                continue
            if finding.severity.rank < existing.finding.severity.rank:
                existing.finding = existing.finding.model_copy(
                    update={"severity": finding.severity}
                )
            agent = finding.agent or run.agent
            if agent not in existing.agents:
                existing.agents.append(agent)
    merged.sort(key=lambda m: sort_key(m.finding))
    return merged


def _safe(text: str) -> str:
    """Neutralise @mentions in model output so a review never pings users or teams."""
    return _MENTION_RE.sub("@\u200b", text)


def _cell(text: str) -> str:
    return _safe(text).replace("|", "\\|").replace("\n", " ")


def _agent_label(agent: str) -> str:
    cls = AGENT_REGISTRY.get(agent)
    return f"{cls.emoji} {cls.title}" if cls else agent


def _render_finding(m: MergedFinding) -> str:
    f = m.finding
    location = f"{f.file}:{f.line}" if f.line else f.file
    lines = [
        f"- {SEVERITY_ICON[f.severity]} **{f.severity.value.upper()}** `{location}` — "
        f"**{_safe(f.title)}**"
    ]
    if f.explanation:
        lines.append("  " + _safe(f.explanation).replace("\n", "\n  "))
    if f.suggestion:
        lines.append("  > 💡 " + _safe(f.suggestion).replace("\n", "\n  > "))
    if len(m.agents) > 1:
        lines.append(f"  <sub>Also flagged by: {', '.join(m.agents[1:])}</sub>")
    return "\n".join(lines)


def _summary_table(runs: list[AgentRun], merged: list[MergedFinding]) -> str:
    rows = [
        "| Agent | Status | Findings | 🔴 Critical | 🟠 High | 🟡 Medium | 🔵 Low | Latency |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for run in runs:
        counts = Counter(m.finding.severity for m in merged if m.agents[0] == run.agent)
        status = "✅ ok" if run.status == AgentStatus.OK else "⚠️ degraded"
        low = counts[Severity.LOW] + counts[Severity.INFO]
        rows.append(
            f"| {_agent_label(run.agent)} | {status} | {sum(counts.values())} | "
            f"{counts[Severity.CRITICAL]} | {counts[Severity.HIGH]} | {counts[Severity.MEDIUM]} | "
            f"{low} | {run.latency_ms / 1000:.1f}s |"
        )
    return "\n".join(rows)


def render_markdown(
    runs: list[AgentRun],
    merged: list[MergedFinding],
    skipped: list[SkippedFile],
    files_reviewed: int,
    *,
    head_sha: str = "",
    now: datetime | None = None,
    max_findings: int | None = None,
) -> str:
    now = now or datetime.now(UTC)
    shown = merged if max_findings is None else merged[:max_findings]
    omitted = len(merged) - len(shown)
    totals = Counter(m.finding.severity for m in merged)
    degraded = [r for r in runs if r.status == AgentStatus.DEGRADED]

    out: list[str] = ["## 🤖 ReviewMind review", ""]
    if not runs:
        out.append("No reviewable changes in this pull request (every file was skipped).")
    else:
        breakdown = " · ".join(
            f"{SEVERITY_ICON[s]} {totals[s]} {s.value}" for s in Severity if totals[s]
        )
        out.append(
            f"**{len(merged)} finding(s)** across **{files_reviewed}** reviewed file(s)"
            + (f" — {breakdown}" if breakdown else "")
        )
        if degraded:
            names = ", ".join(f"**{r.agent}**" for r in degraded)
            out += [
                "",
                f"> ⚠️ **Partial review:** {names} degraded. Their findings may be incomplete; "
                "see *Degraded agents* below.",
            ]
        out += ["", _summary_table(runs, merged)]

        for run in runs:
            section = [m for m in shown if m.agents[0] == run.agent]
            out += ["", f"### {_agent_label(run.agent)} ({len(section)})"]
            if section:
                major = [m for m in section if m.finding.severity.rank <= Severity.MEDIUM.rank]
                minor = [m for m in section if m.finding.severity.rank > Severity.MEDIUM.rank]
                out += [_render_finding(m) for m in major]
                if minor:
                    # Low/info findings are collapsed so they don't bury the important ones.
                    out += ["", f"<details><summary>{len(minor)} low-severity finding(s)</summary>"]
                    out += ["", *[_render_finding(m) for m in minor], "", "</details>"]
            elif run.status == AgentStatus.DEGRADED:
                out.append("_No results: this agent degraded (see below)._")
            else:
                out.append("✅ No issues found.")

        if omitted:
            out += [
                "",
                f"_{omitted} lower-severity finding(s) omitted to fit GitHub's comment size limit. "
                "The full list is available from `GET /reviews/{id}`._",
            ]

        if degraded:
            out += ["", "### ⚠️ Degraded agents", "", "| Agent | Reason |", "|---|---|"]
            out += [
                f"| {_agent_label(r.agent)} | {_cell(r.error or 'unknown error')} |"
                for r in degraded
            ]

    if skipped:
        out += ["", f"<details><summary>Skipped files ({len(skipped)})</summary>", ""]
        out += [f"- `{s.filename}` — {s.reason}" for s in skipped]
        out += ["", "</details>"]

    sha = f" · head `{head_sha[:7]}`" if head_sha else ""
    out += ["", "---", f"<sub>ReviewMind {APP_VERSION}{sha} · {now:%Y-%m-%d %H:%M} UTC</sub>"]
    return "\n".join(out)


def aggregate(
    runs: list[AgentRun],
    skipped: list[SkippedFile],
    files_reviewed: int,
    *,
    head_sha: str = "",
    now: datetime | None = None,
) -> ReviewResult:
    """Build the final ``ReviewResult``.

    If the Markdown would exceed GitHub's body limit, the rendered list is halved until it fits.
    """
    merged = merge_findings(runs)
    limit: int | None = None
    markdown = render_markdown(runs, merged, skipped, files_reviewed, head_sha=head_sha, now=now)
    while len(markdown) > MAX_BODY_CHARS:
        limit = max(0, (len(merged) if limit is None else limit) // 2)
        markdown = render_markdown(
            runs, merged, skipped, files_reviewed, head_sha=head_sha, now=now, max_findings=limit
        )
        if limit == 0:
            markdown = markdown[:MAX_BODY_CHARS]
            break
    findings = [m.finding.model_copy(update={"agent": m.agents[0]}) for m in merged]
    return ReviewResult(
        findings=findings,
        agent_runs=runs,
        skipped_files=skipped,
        files_reviewed=files_reviewed,
        markdown=markdown,
    )
