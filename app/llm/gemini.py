"""Gemini client that calls the Generative Language REST API directly with httpx.

I call the REST endpoint instead of using an SDK on purpose: the old ``google-generativeai``
package is deprecated, and a plain HTTP call is natively async, has no extra dependencies, and
can be unit-tested with ``httpx.MockTransport``.

Endpoint: ``POST {base_url}/models/{model}:generateContent``
"""

from __future__ import annotations

from typing import Any

import httpx

from app.llm.base import LLMClient, LLMError, LLMRequest, LLMResponse

_NON_RETRYABLE = {400, 401, 403, 404}


class GeminiClient(LLMClient):
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.0-flash",
        *,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        temperature: float = 0.2,
        timeout_seconds: float = 90.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("GeminiClient requires an API key")
        self.model = model
        self.temperature = temperature
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            # Header auth keeps the key out of URLs (and therefore out of logs and tracebacks).
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            timeout=timeout_seconds,
            transport=transport,
        )

    def _payload(self, request: LLMRequest) -> dict[str, Any]:
        generation_config: dict[str, Any] = {"temperature": self.temperature}
        if request.json_mode:
            generation_config["responseMimeType"] = "application/json"
        return {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": [{"role": "user", "parts": [{"text": request.prompt}]}],
            "generationConfig": generation_config,
        }

    async def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            resp = await self._http.post(
                f"/models/{self.model}:generateContent", json=self._payload(request)
            )
        except httpx.TimeoutException as exc:
            raise LLMError(f"Gemini request timed out: {type(exc).__name__}") from exc
        except httpx.TransportError as exc:
            raise LLMError(f"Gemini transport error: {type(exc).__name__}") from exc

        if resp.status_code != 200:
            raise LLMError(
                f"Gemini HTTP {resp.status_code}: {_error_message(resp)}",
                retryable=resp.status_code not in _NON_RETRYABLE,
                status_code=resp.status_code,
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise LLMError("Gemini returned a non-JSON HTTP body") from exc

        candidates = body.get("candidates") or []
        if not candidates:
            reason = (body.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise LLMError(f"Gemini returned no candidates ({reason})", retryable=False)
        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        if not text:
            raise LLMError(
                f"Gemini returned an empty response (finishReason={candidate.get('finishReason')})"
            )
        usage = body.get("usageMetadata") or {}
        return LLMResponse(
            text=text,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
        )

    async def aclose(self) -> None:
        await self._http.aclose()


def _error_message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error", {}).get("message", ""))[:300]
    except ValueError:
        return resp.text[:300]
