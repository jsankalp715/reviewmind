"""HMAC-SHA256 signature verification: unit level and through the HTTP endpoint."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.github.webhook import compute_signature, verify_signature
from tests.conftest import FakeGitHub, signed_headers

BODY = b'{"action":"opened","number":1}'


class TestVerifySignature:
    def test_valid_signature(self) -> None:
        sig = "sha256=" + hmac.new(b"s3cret", BODY, hashlib.sha256).hexdigest()
        assert verify_signature("s3cret", BODY, sig)

    def test_compute_signature_matches_github_format(self) -> None:
        sig = compute_signature("s3cret", BODY)
        assert sig.startswith("sha256=") and len(sig) == len("sha256=") + 64

    def test_invalid_signature(self) -> None:
        assert not verify_signature("s3cret", BODY, "sha256=" + "0" * 64)

    def test_wrong_secret(self) -> None:
        assert not verify_signature("s3cret", BODY, compute_signature("other", BODY))

    @pytest.mark.parametrize("header", [None, "", "sha256=", "sha1=abc", "garbage"])
    def test_missing_or_malformed_header(self, header: str | None) -> None:
        assert not verify_signature("s3cret", BODY, header)

    def test_tampered_body(self) -> None:
        sig = compute_signature("s3cret", BODY)
        assert not verify_signature("s3cret", BODY.replace(b"opened", b"closed"), sig)

    def test_empty_secret_never_verifies(self) -> None:
        assert not verify_signature("", BODY, compute_signature("", BODY))

    def test_reserialised_json_does_not_verify(self) -> None:
        """Signing must cover the raw bytes; re-serialising changes whitespace."""
        raw = b'{"action": "opened",  "number": 1}'
        sig = compute_signature("s3cret", raw)
        reserialised = json.dumps(json.loads(raw)).encode()
        assert reserialised != raw
        assert not verify_signature("s3cret", reserialised, sig)
        assert verify_signature("s3cret", raw, sig)


class TestWebhookEndpointSecurity:
    def test_valid_signature_is_accepted(
        self, client: TestClient, pr_payload: dict[str, Any], fake_github: FakeGitHub
    ) -> None:
        body = json.dumps(pr_payload).encode()
        resp = client.post("/webhook/github", content=body, headers=signed_headers(body))
        assert resp.status_code == 202
        assert resp.json()["status"] == "queued"

    def test_missing_signature_returns_401(
        self, client: TestClient, pr_payload: dict[str, Any], fake_github: FakeGitHub
    ) -> None:
        body = json.dumps(pr_payload).encode()
        headers = signed_headers(body)
        del headers["X-Hub-Signature-256"]
        resp = client.post("/webhook/github", content=body, headers=headers)
        assert resp.status_code == 401
        assert fake_github.requests == []  # nothing was processed

    def test_invalid_signature_returns_401(
        self, client: TestClient, pr_payload: dict[str, Any]
    ) -> None:
        body = json.dumps(pr_payload).encode()
        headers = signed_headers(body, secret="wrong-secret")
        assert client.post("/webhook/github", content=body, headers=headers).status_code == 401

    def test_tampered_body_returns_401(
        self, client: TestClient, pr_payload: dict[str, Any], fake_github: FakeGitHub
    ) -> None:
        body = json.dumps(pr_payload).encode()
        headers = signed_headers(body)
        tampered = body.replace(b"acme/shop", b"evil/repo")
        resp = client.post("/webhook/github", content=tampered, headers=headers)
        assert resp.status_code == 401
        assert fake_github.reviews == []

    def test_signature_checked_before_event_filtering(self, client: TestClient) -> None:
        """Even ignorable events must be authenticated first."""
        resp = client.post("/webhook/github", content=b"{}", headers={"X-GitHub-Event": "issues"})
        assert resp.status_code == 401

    def test_unconfigured_secret_fails_closed(
        self, make_client: Any, pr_payload: dict[str, Any]
    ) -> None:
        client = make_client(github_webhook_secret=None)
        body = json.dumps(pr_payload).encode()
        resp = client.post("/webhook/github", content=body, headers=signed_headers(body))
        assert resp.status_code == 503

    def test_legacy_path_is_also_protected(self, client: TestClient) -> None:
        resp = client.post("/webhook", content=b"{}", headers={"X-GitHub-Event": "pull_request"})
        assert resp.status_code == 401
