import logging
from datetime import datetime, timezone

from github import Github, GithubException

from config import GITHUB_TOKEN

logger = logging.getLogger(__name__)

_gh: Github | None = None


def _get_github() -> Github:
    global _gh
    if _gh is None:
        _gh = Github(GITHUB_TOKEN)
    return _gh


def get_pr_diff(repo_name: str, pr_number: int) -> str:
    try:
        repo = _get_github().get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        files = pr.get_files()
        parts: list[str] = []
        for f in files:
            parts.append(f"--- a/{f.filename}\n+++ b/{f.filename}")
            if f.patch:
                parts.append(f.patch)
        return "\n".join(parts)
    except GithubException as exc:
        logger.error("Failed to fetch diff for %s#%d: %s", repo_name, pr_number, exc)
        raise


def post_review_comment(repo_name: str, pr_number: int, body: str) -> str:
    try:
        repo = _get_github().get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        comment = pr.create_issue_comment(body)
        logger.info("Posted review comment on %s#%d: %s", repo_name, pr_number, comment.html_url)
        return comment.html_url
    except GithubException as exc:
        logger.error("Failed to post comment on %s#%d: %s", repo_name, pr_number, exc)
        raise


def format_review_comment(
    security: dict,
    performance: dict,
    docs: dict,
    tests: dict,
    num_files: int,
) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def _render_findings(result: dict) -> str:
        findings = result.get("findings", [])
        if not findings:
            return "✅ No issues found"
        lines: list[str] = []
        for f in findings:
            sev = f.get("severity", "low").upper()
            icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(sev, "⚪")
            lines.append(
                f"- {icon} **[{sev}]** `{f.get('line', 'N/A')}` — {f.get('issue', '')}\n"
                f"  > 💡 {f.get('suggestion', '')}"
            )
        return "\n".join(lines)

    tests_empty_msg = "✅ Looks well tested"

    return f"""## 🤖 ReviewMind Analysis

### 🔒 Security
{_render_findings(security)}

### ⚡ Performance
{_render_findings(performance)}

### 📝 Documentation
{_render_findings(docs)}

### 🧪 Tests
{_render_findings(tests) if tests.get("findings") else tests_empty_msg}

---
*Reviewed by ReviewMind | {timestamp} | {num_files} files changed*"""
