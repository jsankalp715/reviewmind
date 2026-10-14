"""The demo and live smoke scripts, exercised offline (GitHub mocked, LLM_PROVIDER=mock)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from app.config import Settings
from tests.conftest import FakeGitHub

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"scripts_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_happy_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["demo.py", "--latency", "0"])
    assert load("demo").main() == 0
    out = capsys.readouterr().out
    assert "forged signature  -> HTTP 401" in out
    assert "Signed pull_request webhook      -> HTTP 202" in out
    assert "'status': 'duplicate'" in out
    assert "Reviews posted to GitHub: 1" in out
    assert "## 🤖 ReviewMind review" in out


def test_demo_with_faults(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["demo.py", "--latency", "0", "--timeout", "0.1", "--fault", "tests:timeout"]
    )
    assert load("demo").main() == 0
    out = capsys.readouterr().out
    assert "degraded=['tests']" in out
    assert "### ⚠️ Degraded agents" in out


@pytest.fixture
def smoke(monkeypatch: pytest.MonkeyPatch, fake_github: FakeGitHub, tmp_path: Path) -> ModuleType:
    module = load("live_smoke")
    monkeypatch.setattr(module, "github_client", lambda _s: fake_github.client())
    return module


def mock_settings(tmp_path: Path, **kw: Any) -> Settings:
    return Settings(_env_file=None, llm_provider="mock", database_path=str(tmp_path / "s.db"), **kw)


async def test_live_smoke_gemini_mode_with_mock(tmp_path: Path, smoke: ModuleType) -> None:
    assert await smoke.smoke_gemini(mock_settings(tmp_path)) == 0


async def test_live_smoke_review_mode(
    tmp_path: Path, smoke: ModuleType, fake_github: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_github.overrides[r"/repos/acme/shop/pulls/42"] = lambda _r: httpx.Response(
        200,
        json={
            "title": "T",
            "user": {"login": "me"},
            "head": {"sha": fake_github.head_sha, "ref": "f"},
            "base": {"ref": "main"},
        },
    )
    code = await smoke.smoke_review(mock_settings(tmp_path), "acme/shop", 42, post=True)
    assert code == 0
    out = capsys.readouterr().out
    assert "## 🤖 ReviewMind review" in out and "posted: https://github.com/" in out
    assert len(fake_github.reviews) == 1


async def test_live_smoke_webhook_requires_secret(tmp_path: Path, smoke: ModuleType) -> None:
    assert await smoke.smoke_webhook(mock_settings(tmp_path), "acme/shop", 42, "http://x") == 1
