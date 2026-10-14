"""Merge/dedupe/sort and Markdown rendering."""

from __future__ import annotations

from datetime import UTC, datetime

from app.aggregator import MAX_BODY_CHARS, aggregate, merge_findings, render_markdown
from app.models import AgentRun, AgentStatus, Finding, Severity, SkippedFile

NOW = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


def f(
    agent: str, sev: str, title: str, file: str = "a.py", line: int | None = 1, **kw: str
) -> Finding:
    return Finding(agent=agent, severity=sev, title=title, file=file, line=line, **kw)  # type: ignore[arg-type]


def run(
    agent: str, *findings: Finding, status: AgentStatus = AgentStatus.OK, error: str | None = None
) -> AgentRun:
    return AgentRun(
        agent=agent, findings=list(findings), status=status, error=error, latency_ms=1500
    )


def test_sorted_by_severity_then_location() -> None:
    merged = merge_findings(
        [
            run("documentation", f("documentation", "low", "Doc", line=1)),
            run(
                "security",
                f("security", "critical", "Secret", file="z.py", line=9),
                f("security", "medium", "Thing", line=2),
            ),
            run("performance", f("performance", "high", "Slow", file="b.py", line=3)),
        ]
    )
    assert [m.finding.severity for m in merged] == [
        Severity.CRITICAL,
        Severity.HIGH,
        Severity.MEDIUM,
        Severity.LOW,
    ]


def test_exact_duplicates_within_an_agent_collapse() -> None:
    merged = merge_findings(
        [
            run(
                "security",
                f("security", "high", "SQL injection"),
                f("security", "high", "SQL  Injection!"),
            )
        ]
    )
    assert len(merged) == 1


def test_cross_agent_near_duplicates_merge_keeping_highest_severity() -> None:
    merged = merge_findings(
        [
            run("performance", f("performance", "medium", "HTTP call without a timeout", line=5)),
            run("security", f("security", "high", "HTTP call without timeout", line=5)),
        ]
    )
    assert len(merged) == 1
    assert merged[0].finding.severity is Severity.HIGH
    assert merged[0].agents == ["performance", "security"]


def test_different_lines_or_titles_are_not_merged() -> None:
    merged = merge_findings(
        [
            run(
                "security",
                f("security", "high", "SQL injection", line=5),
                f("security", "high", "SQL injection", line=6),
                f("security", "high", "Hardcoded secret", line=5),
            ),
        ]
    )
    assert len(merged) == 3


def test_render_full_review() -> None:
    runs = [
        run(
            "security",
            f(
                "security",
                "critical",
                "Hardcoded secret",
                line=7,
                explanation="Leaked.",
                suggestion="Use env vars.",
            ),
        ),
        run("performance", f("performance", "low", "Minor thing", file="b.py", line=None)),
        run("documentation"),
        run("tests", status=AgentStatus.DEGRADED, error="3/3 chunk(s) failed | timed out"),
    ]
    skipped = [SkippedFile(filename="poetry.lock", reason="lockfile")]
    md = aggregate(runs, skipped, files_reviewed=2, head_sha="abcdef123456", now=NOW).markdown

    assert md.startswith("## 🤖 ReviewMind review")
    assert "**2 finding(s)** across **2** reviewed file(s) — 🔴 1 critical · 🔵 1 low" in md
    assert "| Agent | Status | Findings |" in md
    assert "| 🔒 Security | ✅ ok | 1 | 1 | 0 | 0 | 0 | 1.5s |" in md
    assert "| 🧪 Tests | ⚠️ degraded | 0 |" in md
    assert (
        "- 🔴 **CRITICAL** `a.py:7` — **Hardcoded secret**\n  Leaked.\n  > 💡 Use env vars." in md
    )
    assert "<details><summary>1 low-severity finding(s)</summary>" in md
    assert "`b.py` — **Minor thing**" in md  # no line number -> just the file
    assert "### 📝 Documentation (0)\n✅ No issues found." in md
    assert "### 🧪 Tests (0)\n_No results: this agent degraded (see below)._" in md
    assert "**Partial review:** **tests** degraded" in md
    assert "| 🧪 Tests | 3/3 chunk(s) failed \\| timed out |" in md  # pipe escaped in table
    assert "- `poetry.lock` — lockfile" in md
    assert md.rstrip().endswith("· head `abcdef1` · 2026-01-02 03:04 UTC</sub>")


def test_no_degraded_section_when_all_ok() -> None:
    md = aggregate([run("security")], [], 1, now=NOW).markdown
    assert "Degraded agents" not in md and "Partial review" not in md


def test_nothing_to_review() -> None:
    skipped = [SkippedFile(filename="logo.png", reason="binary file")]
    md = aggregate([], skipped, 0, now=NOW).markdown
    assert "No reviewable changes" in md and "`logo.png` — binary file" in md


def test_mentions_in_model_output_are_neutralised() -> None:
    md = aggregate(
        [run("security", f("security", "high", "Ping @octocat and @acme/team"))], [], 1, now=NOW
    ).markdown
    assert "@octocat" not in md and "@​octocat" in md


def test_also_flagged_by_note() -> None:
    runs = [
        run("performance", f("performance", "high", "N+1 query")),
        run("security", f("security", "high", "N+1 query")),
    ]
    md = aggregate(runs, [], 1, now=NOW).markdown
    assert "Also flagged by: security" in md


def test_huge_reviews_are_truncated_under_github_limit() -> None:
    findings = [
        f("security", "medium", f"Issue number {i}", line=i, explanation="x" * 400)
        for i in range(1, 400)
    ]
    result = aggregate([run("security", *findings)], [], 1, now=NOW)
    assert len(result.markdown) <= MAX_BODY_CHARS
    assert "omitted to fit GitHub's comment size limit" in result.markdown
    assert len(result.findings) == 399  # nothing is lost from the stored result


def test_render_markdown_uses_current_time_by_default() -> None:
    md = render_markdown([run("security")], [], [], 1)
    assert f"{datetime.now(UTC):%Y-%m-%d}" in md


def test_result_findings_carry_primary_agent() -> None:
    result = aggregate([run("tests", f("", "low", "No tests"))], [], 1, now=NOW)
    assert result.findings[0].agent == "tests"
    assert result.degraded_agents == []
