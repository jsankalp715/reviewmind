"""GeminiClient (against an httpx MockTransport) and LLM client selection."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.llm import GeminiClient, LLMError, LLMRequest, MockLLMClient, build_llm_client

REQ = LLMRequest(system="You review code.", prompt="diff here", agent="security")


def gemini(handler: Any) -> GeminiClient:
    return GeminiClient("key-123", "gemini-2.0-flash", transport=httpx.MockTransport(handler))


def ok_body(text: str = '{"findings": []}') -> dict[str, Any]:
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 30},
    }


async def test_request_shape_and_response_parsing() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=ok_body('{"findings": [], "summary": "ok"}'))

    client = gemini(handler)
    resp = await client.complete(REQ)
    await client.aclose()

    req = seen[0]
    assert req.url.path == "/v1beta/models/gemini-2.0-flash:generateContent"
    assert req.headers["x-goog-api-key"] == "key-123"
    assert "key-123" not in str(req.url)  # the key never appears in URLs or logs
    body = json.loads(req.content)
    assert body["systemInstruction"]["parts"][0]["text"] == "You review code."
    assert body["contents"] == [{"role": "user", "parts": [{"text": "diff here"}]}]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert resp.text == '{"findings": [], "summary": "ok"}'
    assert (resp.input_tokens, resp.output_tokens, resp.total_tokens) == (120, 30, 150)


async def test_multi_part_text_is_joined() -> None:
    body = ok_body()
    body["candidates"][0]["content"]["parts"] = [{"text": '{"findings": '}, {"text": "[]}"}]
    resp = await gemini(lambda _r: httpx.Response(200, json=body)).complete(REQ)
    assert resp.text == '{"findings": []}'


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(400, False), (401, False), (403, False), (404, False), (429, True), (500, True), (503, True)],
)
async def test_http_errors(status: int, retryable: bool) -> None:
    client = gemini(lambda _r: httpx.Response(status, json={"error": {"message": "boom"}}))
    with pytest.raises(LLMError) as info:
        await client.complete(REQ)
    assert info.value.status_code == status
    assert info.value.retryable is retryable
    assert "boom" in str(info.value)


async def test_blocked_prompt_is_not_retryable() -> None:
    body = {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}
    with pytest.raises(LLMError, match="SAFETY") as info:
        await gemini(lambda _r: httpx.Response(200, json=body)).complete(REQ)
    assert info.value.retryable is False


async def test_empty_candidate_text() -> None:
    body = {"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]}
    with pytest.raises(LLMError, match="MAX_TOKENS"):
        await gemini(lambda _r: httpx.Response(200, json=body)).complete(REQ)


async def test_non_json_http_body() -> None:
    with pytest.raises(LLMError, match="non-JSON"):
        await gemini(lambda _r: httpx.Response(200, text="<html>")).complete(REQ)


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("t"), httpx.ConnectError("c")])
async def test_transport_errors_become_llm_errors(exc: Exception) -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        raise exc

    with pytest.raises(LLMError) as info:
        await gemini(handler).complete(REQ)
    assert info.value.retryable


async def test_error_body_that_is_not_json() -> None:
    with pytest.raises(LLMError, match="upstream exploded"):
        await gemini(lambda _r: httpx.Response(502, text="upstream exploded")).complete(REQ)


def test_gemini_requires_key() -> None:
    with pytest.raises(ValueError):
        GeminiClient("")


def test_settings_require_key_for_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValidationError, match="GEMINI_API_KEY"):
        Settings(_env_file=None, llm_provider="gemini")


async def test_factory_selects_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_LLM_FAULTS", "tests:garbage")
    mock = build_llm_client(Settings(_env_file=None, llm_provider="mock"))
    assert isinstance(mock, MockLLMClient) and mock.faults == {"tests": "garbage"}

    real = build_llm_client(
        Settings(_env_file=None, llm_provider="gemini", gemini_api_key="k", gemini_model="m-1")
    )
    assert isinstance(real, GeminiClient) and real.model == "m-1"
    await real.aclose()


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("CHUNK_MAX_TOKENS", "1000")
    monkeypatch.setenv("ENABLED_AGENTS", '["security"]')
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "abc")
    s = Settings(_env_file=None)
    assert s.chunk_max_chars == 4000
    assert s.enabled_agents == ["security"]
    assert s.webhook_secret == "abc"
    assert "abc" not in repr(s)  # secrets are masked in reprs/logs
    assert Settings(_env_file=None, github_webhook_secret="").webhook_secret is None
