# Summary: overnight upgrade of ReviewMind

Branch: **`upgrade/production-ready`** (based on `main`, no force-pushes, no PR opened).
Commits are authored as *Sankalp Jain &lt;jsankalp715@gmail.com&gt;* with no co-author trailers
(see DECISIONS.md P2 if you'd rather use your TCD address).

## 1. What the audit found (full detail: [AUDIT.md](AUDIT.md))

- **Security:** the webhook **failed open**. With no `GITHUB_WEBHOOK_SECRET`, unsigned requests
  were accepted (I reproduced a 200).
- **The "chunked per file" claim was false.** The diff was **truncated** at 16,000 characters and
  everything after that was silently dropped.
- **"Fault isolation" was partial.** Errors were caught, but a failed agent showed as
  "✅ No issues found" (a silent false negative). There were no timeouts and no retry on bad JSON.
- **Blocking I/O in the request path.** The diff was fetched with synchronous PyGithub *before*
  responding, risking GitHub's 10-second timeout, which triggers redeliveries and therefore
  duplicate reviews. There was **no idempotency**.
- The response was 200 instead of 202, the route was `/webhook`, and `reopened` was ignored.
- **`.gitignore` was UTF-16 encoded**, so git ignored nothing. 25 `.pyc` files and the (empty)
  SQLite DB were committed.
- Other gaps: Gemini was configured at import time (untestable offline); deprecated
  `google-generativeai`; no tests, CI, settings validation or structured logs; the Dockerfile ran
  as root and would have baked `.env` into the image.

### Secrets check

I scanned **all git history** for Google (`AIza…`), GitHub (`ghp_…`, `github_pat_…`), `sk-…` and
private-key patterns. **No real secrets found.** The only hits are the README placeholder
`ghp_your_github_pat_here` and the deliberately fake `API_KEY = "sk-prod-abcdef…"` /
`DB_PASSWORD` in the sample diff, which the security agent is meant to catch. `.env` was never
committed. **Nothing to rotate.**

## 2. What I changed

The code is restructured into `app/` (`agents/`, `llm/`, `github/`) and `tests/`. Every old
module was replaced, not just deleted (see the mapping in DECISIONS.md).

| Spec item | Where | Status |
|---|---|---|
| `POST /webhook/github`, HMAC-SHA256 over raw bytes, `compare_digest`, 401 | `app/main.py`, `app/github/webhook.py` | ✅ It also fails closed (503) when no secret is set. |
| Only `pull_request` opened/synchronize/reopened; others 200 | `app/main.py` | ✅ `ping` → 200 pong |
| 202 immediately + background task | `app/main.py` → `ReviewPipeline.run` | ✅ |
| Idempotent on `X-GitHub-Delivery` | `ReviewRepository.claim_delivery` (UNIQUE column) | ✅ Race-safe. Failed reviews can be retried by redelivery. |
| Async httpx GitHub client: diff + files, pagination, retry/backoff, post review | `app/github/client.py` | ✅ It honours `Retry-After` / `X-RateLimit-Reset`, and POSTs are never retried after a possible success. |
| Chunk per file → per hunk → per line; skip binaries/lockfiles | `app/chunking.py` | ✅ Skipped files are listed in the review. |
| 4 agents, own prompts, Pydantic findings (file, line, severity, title, explanation, suggestion) | `app/agents/`, `app/models.py` | ✅ |
| `gather(return_exceptions=True)`, per-agent timeouts, Semaphore | `app/orchestrator.py`, `app/agents/base.py` | ✅ |
| Retry once → degraded; others complete; review names degraded agents | `ReviewAgent.review_chunk`, `app/aggregator.py` | ✅ |
| `LLMClient`, `GeminiClient`, `MockLLMClient`, `LLM_PROVIDER` | `app/llm/` | ✅ Gemini is called over REST with httpx. |
| JSON repair + Pydantic | `app/llm/json_repair.py` | ✅ |
| Merge, dedupe, severity sort, summary table, per-agent sections, degraded section | `app/aggregator.py` | ✅ |
| SQLite reviews / agent_runs / findings; `/health`, `/reviews`, `/reviews/{id}` | `app/db.py`, `app/main.py` | ✅ |
| Pydantic Settings, `.env.example`, structured logs, Dockerfile, compose, CI | `app/config.py`, `app/logging_config.py`, repo root, `.github/workflows/ci.yml` | ✅ |
| `scripts/demo.py`, `scripts/live_smoke.py` | `scripts/` | ✅ |
| README (Mermaid, setup, webhook, design decisions), INTERVIEW_NOTES | repo root | ✅ |

Additions beyond the spec, all small and each recorded in DECISIONS.md:
- Head-SHA re-check before posting (`superseded`).
- Crash recovery on startup.
- Optional `API_TOKEN` for the read API.
- 413 for oversized bodies.
- `@mention` neutralisation.
- Collapsing low-severity findings.
- Fitting the review under GitHub's 65,536-character limit.
- Safe migration of the old DB schema (tables renamed, not dropped).

## 3. What is verified, and how

Everything below is verified **offline**: GitHub is an `httpx.MockTransport`, and the LLM is
`MockLLMClient` or a `MockTransport` that imitates Gemini's REST responses.

- **231 tests pass** on Python 3.13 and 3.11 (`pytest`). Line coverage of `app/` is **97%**.
  Branch coverage is included; CI fails below 85%.
- `ruff check .` and `ruff format --check .` are clean.
- Covered specifically:
  - Signatures: valid, invalid, missing, malformed, tampered, re-serialised, and no secret
    configured.
  - Chunking: oversized files split by hunk and by line, skipped files.
  - Each agent's happy path.
  - Fault isolation: raise, timeout, garbage JSON, and a crash inside the agent, both at unit
    level and through the webhook.
  - Duplicate deliveries: sequential, and 8 concurrent.
  - End to end: signed webhook + fixture diff → Markdown posted to the mocked GitHub.
  - GitHub retry/pagination/rate-limit behaviour.
  - Gemini request and response handling.
  - DB migration and crash recovery.
- **Bugs that tests or the self-review caught and I fixed:**
  - The test-settings helper skipped validation, so secrets stayed as `str`.
  - Very long hunk headers could break the line-split budget.
  - The degraded reason repeated the same error once per chunk, and included raw model output in
    the public review.
  - A single DB failure could escape the background task.
- `scripts/demo.py` was run, and its real output is in the README.
- The Docker image was built and run locally: it starts as uid 10001, logs JSON, and `/health`
  returns 200. To get past this sandbox's TLS-intercepting proxy I built from a scratch copy of
  the Dockerfile that only added the proxy CA. The committed Dockerfile is unchanged.

## 4. What is NOT verified against live APIs

This sandbox cannot reach Gemini, and I did not run the app against your GitHub repos.

- **Gemini:** I wrote the request and response format from the public `generateContent` REST
  API (`x-goog-api-key` header, `systemInstruction`, `generationConfig.responseMimeType`,
  `candidates[].content.parts[].text`, `usageMetadata`) and tested it only against mocks. **It is
  not verified that `gemini-2.0-flash` is still served.** Google retires models; if you get a 404,
  set `GEMINI_MODEL=gemini-2.5-flash`.
- **Real review quality.** The prompts were not tuned against real Gemini output. The mock
  reviewer is a regex heuristic, so the demo output shows the *pipeline* works, not how good the
  reviews are.
- **GitHub:** the Reviews API post (`event: COMMENT`, `commit_id`), the 422 → issue-comment
  fallback, pagination over real `Link` headers, real rate-limit headers, and your token's
  permissions.
- **Webhook relays** (smee.io or ngrok) and the CI workflow running on GitHub. CI should run on
  this push, because the workflow triggers on `upgrade/**`.

## 5. Running it tomorrow with your real keys

```bash
git fetch origin && git checkout upgrade/production-ready
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest                                     # offline sanity check

cp .env.example .env
#   GEMINI_API_KEY=...                      (https://aistudio.google.com/app/apikey)
#   GITHUB_TOKEN=...                        fine-grained PAT on the target repo:
#                                           Pull requests = Read and write, Contents = Read
#   GITHUB_WEBHOOK_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
#   LOG_FORMAT=text                         (easier to read locally)

python scripts/live_smoke.py gemini        # 1. key + model work? (prints a hint on 404)
python scripts/live_smoke.py review --repo OWNER/REPO --pr N      # 2. real review, prints only
python scripts/live_smoke.py review --repo OWNER/REPO --pr N --post   # 3. post it to the PR

uvicorn app.main:app --port 8000           # 4. run the server
ngrok http 8000                            #    or: npx smee-client --url https://smee.io/<ch> \
                                           #          --target http://localhost:8000/webhook/github
```

Then in **GitHub → repo → Settings → Webhooks → Add webhook**:
- **Payload URL:** `https://<ngrok-or-smee-url>/webhook/github`
- **Content type:** `application/json`
- **Secret:** the same `GITHUB_WEBHOOK_SECRET`
- **Events:** *Pull requests* only

The `ping` delivery should show ✓. Push a commit to a PR and watch the logs, or run
`curl localhost:8000/reviews`. You can also replay a signed webhook for a real PR against your
server without touching GitHub's settings:
`python scripts/live_smoke.py webhook --repo OWNER/REPO --pr N`.

Docker alternative: `docker compose up --build` (it reads `.env` and keeps the DB in a volume).

## 6. Suggested follow-ups (not done)

- Inline review comments on specific diff lines (the findings already carry `file`/`line`).
- A job queue (Celery, RQ or a cloud queue) instead of in-process `BackgroundTasks`, for
  multi-instance deployments.
- Batching small files into one LLM call to cut call count.
- Webhook secret rotation (accept two secrets during a rollover).
- Open a PR from `upgrade/production-ready` → `main` once you've done the live smoke test.
