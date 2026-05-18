import asyncio
import json
import logging
import re
from abc import ABC
from pathlib import Path

import google.generativeai as genai

from config import GEMINI_API_KEY, GEMINI_MODEL, MAX_RETRIES, RETRY_BASE_DELAY

logger = logging.getLogger(__name__)

genai.configure(api_key=GEMINI_API_KEY)

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


class BaseAgent(ABC):
    name: str = "base"
    prompt_file: str = ""

    def __init__(self) -> None:
        self._system_prompt = (_PROMPTS_DIR / self.prompt_file).read_text(encoding="utf-8")
        self._model = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction=self._system_prompt,
        )

    async def review(self, diff: str, context: dict) -> dict:
        user_message = self._build_user_message(diff, context)
        raw = await self._call_with_retry(user_message)
        return self._parse_response(raw)

    def _build_user_message(self, diff: str, context: dict) -> str:
        return (
            f"PR Title: {context.get('pr_title', 'N/A')}\n"
            f"Repository: {context.get('repo_full_name', 'N/A')}\n\n"
            f"--- DIFF START ---\n{diff}\n--- DIFF END ---"
        )

    async def _call_with_retry(self, message: str) -> str:
        last_exc: Exception = RuntimeError("No attempts made")
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await asyncio.to_thread(
                    self._model.generate_content, message
                )
                return response.text
            except Exception as exc:
                last_exc = exc
                wait = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "[%s] Attempt %d/%d failed (%s). Retrying in %.1fs…",
                    self.name, attempt, MAX_RETRIES, exc, wait,
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(wait)
        raise last_exc

    def _parse_response(self, raw: str) -> dict:
        try:
            text = raw.strip()
            # Strip markdown code fences if present
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            data = json.loads(text)
            findings = [
                {
                    "line": str(f.get("line", "N/A")),
                    "issue": str(f.get("issue", "")),
                    "suggestion": str(f.get("suggestion", "")),
                    "severity": f.get("severity", "low") if f.get("severity") in ("low", "medium", "high") else "low",
                }
                for f in data.get("findings", [])
            ]
            severity = self._overall_severity(findings)
            return {
                "agent": self.name,
                "findings": findings,
                "severity": severity,
                "summary": str(data.get("summary", "")),
            }
        except Exception as exc:
            logger.error("[%s] Failed to parse response: %s\nRaw:\n%s", self.name, exc, raw[:500])
            return {
                "agent": self.name,
                "findings": [],
                "severity": "low",
                "summary": f"Agent encountered a parsing error: {exc}",
            }

    @staticmethod
    def _overall_severity(findings: list[dict]) -> str:
        for sev in ("high", "medium", "low"):
            if any(f["severity"] == sev for f in findings):
                return sev
        return "low"

