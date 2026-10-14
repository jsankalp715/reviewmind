#!/usr/bin/env python
"""Replay a signed GitHub ``pull_request`` webhook against the real app, fully offline.

What is real: the FastAPI app, HMAC verification, idempotency, SQLite, chunking, the four
agents, the orchestrator, and the aggregator.
What is faked: the LLM (``MockLLMClient``, a deterministic heuristic reviewer) and GitHub (an
``httpx.MockTransport`` that serves ``tests/fixtures/sample_pr.diff`` and records the posted
review).

    python scripts/demo.py                                    # happy path
    python scripts/demo.py --fault tests:timeout --fault documentation:garbage
    python scripts/demo.py --latency 0.3                      # watch the agents run in parallel
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
import warnings
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient`")

from fastapi.testclient import TestClient  # noqa: E402

from app.chunking import parse_unified_diff  # noqa: E402
from app.config import Settings  # noqa: E402
from app.github.client import GitHubClient  # noqa: E402
from app.github.webhook import compute_signature  # noqa: E402
from app.llm.mock import MockLLMClient, parse_faults  # noqa: E402
from app.main import create_app  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
DEMO_SECRET = "demo-webhook-secret"


def fake_github(diff_text: str, head_sha: str, posted: list[dict[str, object]]) -> GitHubClient:
    files = []
    for f in parse_unified_diff(diff_text):
        item = f.model_dump(exclude={"previous_filename"})
        if item["patch"] is None:
            item.pop("patch")
        files.append(item)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/files"):
            return httpx.Response(200, json=files)
        if path.endswith("/reviews") and request.method == "POST":
            posted.append(json.loads(request.content))
            return httpx.Response(
                200, json={"html_url": "https://github.com/acme/shop/pull/42#pullrequestreview-1"}
            )
        if re.search(r"/pulls/\d+$", path):
            return httpx.Response(200, json={"head": {"sha": head_sha}})
        return httpx.Response(404, json={"message": "not found"})

    return GitHubClient("demo-token", transport=httpx.MockTransport(handler))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--fault",
        action="append",
        default=[],
        metavar="AGENT:MODE",
        help="inject a mock fault: error | timeout | garbage | flaky",
    )
    parser.add_argument("--latency", type=float, default=0.05, help="mock LLM latency per call (s)")
    parser.add_argument("--timeout", type=float, default=1.0, help="per-attempt agent timeout (s)")
    parser.add_argument("--verbose", action="store_true", help="show the app's JSON logs")
    args = parser.parse_args()

    payload = json.loads((FIXTURES / "pull_request_opened.json").read_text())
    diff_text = (FIXTURES / "sample_pr.diff").read_text()
    posted: list[dict[str, object]] = []

    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            _env_file=None,
            llm_provider="mock",
            github_webhook_secret=DEMO_SECRET,
            database_path=str(Path(tmp) / "demo.db"),
            agent_timeout_seconds=args.timeout,
            agent_retry_backoff_seconds=0.1,
            log_level="INFO" if args.verbose else "ERROR",
            log_format="json",
        )
        llm = MockLLMClient(faults=parse_faults(",".join(args.fault)), latency_seconds=args.latency)
        app = create_app(
            settings,
            llm=llm,
            github=fake_github(diff_text, payload["pull_request"]["head"]["sha"], posted),
        )

        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "demo-delivery-0001",
            "X-Hub-Signature-256": compute_signature(DEMO_SECRET, body),
        }
        with TestClient(app) as client:
            bad = client.post(
                "/webhook/github",
                content=body,
                headers={**headers, "X-Hub-Signature-256": "sha256=" + "0" * 64},
            )
            print(f"[1] Webhook with a forged signature  -> HTTP {bad.status_code} {bad.json()}")

            started = time.perf_counter()
            resp = client.post("/webhook/github", content=body, headers=headers)
            elapsed = time.perf_counter() - started
            print(f"[2] Signed pull_request webhook      -> HTTP {resp.status_code} {resp.json()}")
            print(f"    (202 sent first; TestClient also waits for the review: {elapsed:.2f}s)")

            dup = client.post("/webhook/github", content=body, headers=headers)
            print(f"[3] Same X-GitHub-Delivery again     -> HTTP {dup.status_code} {dup.json()}")

            review = client.get(f"/reviews/{resp.json()['review_id']}").json()
            print(
                f"[4] GET /reviews/{review['id']}: status={review['status']}, "
                f"findings={review['total_findings']}, degraded={review['degraded_agents']}"
            )
            print("    agent_runs:")
            for run in review["agent_runs"]:
                print(
                    f"      - {run['agent']:<13} {run['status']:<9} {run['latency_ms']:>5} ms  "
                    f"attempts={run['attempts']}  tokens~{run['token_estimate']}"
                    + (f"  error={run['error'][:70]!r}" if run["error"] else "")
                )
            print(f"    LLM calls: {len(llm.calls)}, peak concurrent calls: {llm.max_in_flight}")

    print(f"[5] Reviews posted to GitHub: {len(posted)}\n")
    print("=" * 100)
    print(posted[0]["body"] if posted else "(nothing posted)")
    print("=" * 100)
    return 0 if len(posted) == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
