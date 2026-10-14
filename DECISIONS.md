# Decisions log

Decisions I made while you were offline, with the reasoning, so you can reverse any of them.
They are grouped by area, not by time.

## Process

| # | Decision | Why |
|---|---|---|
| P1 | Work on `upgrade/production-ready` as you asked. Never force-push. | Your instructions. (The cloud session also set up a `claude/...` branch; I pushed nothing there.) |
| P2 | Commits are authored as **Sankalp Jain &lt;jsankalp715@gmail.com&gt;**, with no `Co-Authored-By` trailers. | You asked to be the only contributor. The original commit used `jains8@tcd.ie`. I chose the Gmail address because it matches your GitHub username and account email, so GitHub should credit the commits to your profile. If you prefer the TCD address, run `git config user.email jains8@tcd.ie` for future commits. Past commits would need a history rewrite, and I don't force-push. |
| P3 | Refactored into `app/`, `app/agents/`, `app/llm/`, `app/github/`, `tests/`. Old top-level modules are deleted **only because each is replaced** (mapping below). | You allowed this if the structure was a mess. Flat modules with import-time side effects (Gemini configured on import) made the code untestable offline. |
| P4 | Kept your four prompts (moved with `git mv` to keep history). Kept their "Focus on" lists. Replaced only their output-format sections with one shared contract. | The prompts were good. The schema had to change (`file`/`line`/`title`/`explanation`), and defining it once in `app/agents/base.py::OUTPUT_CONTRACT` stops the four from drifting apart. |
| P5 | Kept your sample diff, but regenerated it with real `git diff` as `tests/fixtures/sample_pr.diff`, adding a lockfile and a PNG. | The original hunk headers had wrong line counts (`@@ -1,8 +1,42 @@` over 24 lines), which a correct parser rejects. Same code and same planted bugs. |

Old → new mapping: `main.py` → `app/main.py` (the root `main.py` remains as a compatibility shim);
`webhook.py` → `app/github/webhook.py`; `github_client.py` → `app/github/client.py`;
`orchestrator.py` → `app/orchestrator.py` + `app/pipeline.py`; `database.py` → `app/db.py`;
`models.py` → `app/models.py`; `config.py` → `app/config.py`; `agents/*` → `app/agents/*`;
the `format_review_comment` function → `app/aggregator.py`.

## Webhook and security

| # | Decision | Why |
|---|---|---|
| S1 | **Fail closed:** with no `GITHUB_WEBHOOK_SECRET`, the webhook returns **503** and logs an error at startup. | The old code accepted unsigned requests when the secret was missing. 503 rather than 401, because it is a server misconfiguration, not a bad client. |
| S2 | Verify the signature **before** reading any header or JSON, using the raw `await request.body()` bytes and `hmac.compare_digest`. | The spec requires this. It also means unauthenticated callers learn nothing, not even which events are ignored. |
| S3 | Ignored events and actions return **200** `{"status":"ignored"}`. `ping` returns 200 `pong`. A malformed signed payload returns 400 (bad JSON) or 422 (missing fields). | GitHub shows non-2xx responses as failed deliveries. Ignoring is not a failure. |
| S4 | Handled actions are `opened`, `synchronize`, `reopened`. I did **not** add `ready_for_review` or skip drafts. | Kept to the spec. It is easy to add in `HANDLED_ACTIONS`. |
| S5 | Idempotency key is `X-GitHub-Delivery`, enforced by a `UNIQUE` column. A missing delivery header returns **400**. | The database makes the atomic decision, so it is race-free across concurrent requests and threads (tested with 8 parallel duplicates). |
| S6 | A redelivery is allowed to re-run only if the earlier attempt **failed** (atomic `UPDATE ... WHERE status='failed'`). Reviews still `queued`/`running` at startup are marked `failed` ("interrupted by restart"). | Without this, GitHub's "Redeliver" button could never recover a crashed review. Completed reviews are never re-run. |
| S7 | Added an optional `API_TOKEN` (Bearer auth) for `GET /reviews*`. | Reviews describe private code. It is optional so local development stays simple. Startup logs a warning when it is unset. |
| S8 | 413 for webhook bodies over 25 MB (GitHub's own cap). | Stops the server buffering arbitrarily large bodies. |
| S9 | `@mentions` in LLM output are neutralised (zero-width space) before posting. | Prompt injection in a diff could otherwise make the bot ping people or teams. |
| S10 | The system prompt marks the diff as untrusted input. Model output only reaches GitHub as Markdown in a review body. | It is a basic prompt-injection mitigation. Don't overclaim it: it does not stop a model from being influenced. |

## GitHub client

| # | Decision | Why |
|---|---|---|
| G1 | Use `httpx.AsyncClient` directly. PyGithub was dropped. | PyGithub is synchronous and blocked the event loop. The spec asks for httpx. |
| G2 | Main source of the diff is `GET /pulls/{n}/files` (paginated, `per_page=100`, follows `Link: rel="next"`, at most 30 pages = GitHub's 3,000-file cap). For text files where GitHub omits `patch` (very large files), the full `.diff` is fetched and parsed. | The files API gives per-file patches, which is exactly what per-file chunking needs. |
| G3 | Retries: GETs retry on 429/5xx/rate-limit 403/transport errors. **POSTs retry only on rate-limit responses or connect errors.** | Retrying a POST after a 502 or a read timeout could post the review twice. |
| G4 | Honour `Retry-After` and `X-RateLimit-Reset`. Otherwise use exponential backoff with jitter. If GitHub asks for a wait longer than `GITHUB_BACKOFF_MAX_SECONDS` (60), fail fast. | Avoids holding a background task open for up to an hour on a primary rate limit. |
| G5 | Post as a PR **review** (`event: COMMENT`, `commit_id` = head SHA). On 422, fall back to an issue comment. | It is a review, not a chat comment, and `COMMENT` works even on your own PRs. The 422 fallback covers a stale `commit_id`. |
| G6 | Before posting, re-check the PR head SHA. If it moved, store the review as `superseded` and don't post. | The newer `synchronize` delivery produces its own review, so posting a stale one is noise. |

## Chunking

| # | Decision | Why |
|---|---|---|
| C1 | One chunk per file. If a file's annotated diff exceeds `CHUNK_MAX_TOKENS × CHARS_PER_TOKEN` (default 6000 × 4 = 24,000 chars), split it on hunk boundaries and greedily pack the hunks. A single oversized hunk is split by lines, repeating the hunk header. | This is the spec. Also, a chunk never mixes files, so findings can be pinned to the right path. |
| C2 | The token count is **estimated** as characters ÷ 4. There is no tokenizer. | It avoids a dependency and a network call. Real Gemini token counts from `usageMetadata` are recorded in `agent_runs.token_estimate` when available. |
| C3 | Each diff line is prefixed with its **new-file line number** before it is sent to the model. | LLMs are bad at computing line numbers from `@@` headers. Prefixing them makes `line` reliable. |
| C4 | Skipped (and listed in the review): lockfiles, binaries (by extension or missing patch), minified/map files, vendored dirs, deleted files, empty patches. | This is the spec, plus obvious noise. |
| C5 | `MAX_CHUNKS_PER_REVIEW=40` caps cost. Files past the cap are listed as skipped with a reason. | The old code truncated silently. Now the review says exactly what it didn't read. |
| C6 | Small files are **not** packed together into one LLM call. | Spec fidelity ("per file"), and every finding maps to one file. The cost is more calls on PRs with many small files. It is listed in SUMMARY as a possible follow-up. |

## Agents, LLM, fault isolation

| # | Decision | Why |
|---|---|---|
| A1 | `GeminiClient` calls the REST API (`:generateContent`) with httpx instead of an SDK. | `google-generativeai` is deprecated. REST is natively async, has no new dependency, and can be tested with `MockTransport`. The API key goes in the `x-goog-api-key` header, never in the URL. |
| A2 | Default model is `gemini-2.0-flash` (it matches your CV). It can be configured with `GEMINI_MODEL`. | **I could not verify from here whether Google has retired 2.0 Flash.** If the smoke test returns 404, set `GEMINI_MODEL=gemini-2.5-flash`. `scripts/live_smoke.py gemini` prints this hint. |
| A3 | `LLM_PROVIDER` defaults to **`gemini`**, and startup fails if there is no key. | Defaulting to `mock` could silently post fake reviews to real PRs in production. |
| A4 | Retry policy for each chunk call: 1 attempt + `AGENT_MAX_RETRIES` (default 1) on exception, timeout, or unrepairable JSON. A JSON failure re-prompts with a "reply with ONLY the JSON" hint. **Non-retryable** provider errors (400/401/403/404, blocked prompt) are not retried. | This is the spec ("retry once, then degrade"), except that retrying a bad API key is pointless. That one deviation is deliberate and tested. |
| A5 | The timeout (`AGENT_TIMEOUT_SECONDS`, default 60) applies to each attempt and wraps only the LLM call, **inside** the semaphore. | Time spent queued for a slot shouldn't count as the model being slow. |
| A6 | One `asyncio.Semaphore(LLM_MAX_CONCURRENCY=4)` is shared by all agents **and** all concurrent reviews. | It bounds total in-flight calls against provider rate limits, not just the calls inside one review. |
| A7 | If some chunks fail, the agent is `degraded` but **keeps** findings from its successful chunks. | It degrades gracefully instead of all-or-nothing. The review states the agent is partial. |
| A8 | Two layers of isolation: the agent catches its own expected errors, and the orchestrator uses `gather(return_exceptions=True)` for unexpected crashes. | Both layers are tested separately. |
| A9 | Invalid individual findings are dropped. If **all** findings in a non-empty list are invalid, the response counts as invalid (and is retried). | One bad item shouldn't discard nine good ones. All-bad usually means the model ignored the schema. |
| A10 | The finding's `file` is overwritten with the chunk's file. Severity aliases (`warning`→medium, `blocker`→critical…) and line strings (`"12-15"`, `"app.py:7"`) are normalised. | Common model sloppiness, fixed during validation instead of failing the call. |
| A11 | Severity scale: `critical/high/medium/low/info`. | The spec says "severity". `critical` separates "exploitable now" from "bad". |
| A12 | `MockLLMClient` is a deterministic regex-heuristic reviewer with fault injection (`error`, `timeout`, `garbage`, `flaky`). | Tests and the demo need realistic, stable output offline. **It is not a real reviewer**, and the README says so. |

## Aggregation and storage

| # | Decision | Why |
|---|---|---|
| D1 | Dedupe: findings on the same file and line whose normalised titles are equal or ≥ 0.82 similar (`difflib`) merge. The merged finding keeps the highest severity and lists "also flagged by". | It catches the same issue reported by two agents with slightly different wording. Deliberately conservative: different lines are never merged. |
| D2 | Low and info findings are collapsed in `<details>` within each agent section. | Keeps the critical and high findings visible. |
| D3 | If the Markdown would exceed GitHub's 65,536-character limit (I target 60,000), the displayed list is halved until it fits. The full list is still stored in SQLite. | The old code had no limit. |
| D4 | Stdlib `sqlite3` via `asyncio.to_thread`, one short-lived connection per operation, WAL mode. No ORM, no aiosqlite. | No new dependency, and no blocking of the event loop. The workload is a few writes per review. |
| D5 | An old v1 database is migrated by **renaming** its tables to `legacy_*_v1`, never dropping them. | Your rule: don't delete existing work. Your committed `reviewmind.db` was empty anyway. |
| D6 | `GET /reviews` (paginated, filter by `repo`, `pr_number`) replaces `/reviews/{owner}/{repo}`. `GET /reviews/{id}` replaces `/reviews/{owner}/{repo}/{pr}`. `POST /webhook` is kept as an alias of `/webhook/github`. | This matches the spec. The alias means an already-registered webhook keeps working. |

## Ops

| # | Decision | Why |
|---|---|---|
| O1 | `pydantic-settings` replaces `python-dotenv` + `os.getenv`. Secrets are `SecretStr`, which is masked in reprs. | This is the spec. Secrets can't leak through a logged settings object. |
| O2 | JSON logs by default (`LOG_FORMAT=text` for humans). Context vars carry `delivery_id`, `review_id`, `repo`, and `pr` into every log line of a review. | This is the spec. Uvicorn's own access log keeps uvicorn's format. |
| O3 | Docker: `python:3.12-slim`, non-root uid 10001, `/data` volume, healthcheck, `.dockerignore` (keeps `.env` and tests out of the image). | The old Dockerfile ran as root and would bake a local `.env` into the image. |
| O4 | CI: ruff lint + format check; pytest on 3.11/3.12/3.13 with `--cov-fail-under=85`; docker build. | This is the spec. |
| O5 | Pinned exact versions in `requirements.txt` (FastAPI 0.142.2, httpx 0.28.1, pydantic 2.13.5, pydantic-settings 2.15.0, uvicorn 0.54.0). | Reproducible builds. |
| O6 | Dropped the "Deploy to Render" README section. | I did not verify it against the new layout. The Docker instructions cover deployment. |
