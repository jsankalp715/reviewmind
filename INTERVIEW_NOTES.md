# Interview notes: ReviewMind

One section per CV bullet. Each has a 30-second explanation that names real files and functions,
the tradeoffs, and five likely questions with answers. Every claim here is true of the code on
`upgrade/production-ready` and is covered by a test (the test file is named where it matters).

---

## 1. "Concurrent pipeline running 4 parallel LLM agents (security, performance, docs, tests) on GitHub PR webhooks"

### 30-second version

> A GitHub `pull_request` webhook hits `POST /webhook/github` in `app/main.py`. The handler only
> verifies the signature, claims the delivery ID in SQLite and returns **202**. The review itself
> runs as a FastAPI background task: `ReviewPipeline.run` in `app/pipeline.py`. It fetches the
> PR's files with an async httpx client, chunks them, and calls `AgentOrchestrator.run`
> (`app/orchestrator.py`). That starts the four agents with
> `asyncio.gather(..., return_exceptions=True)`. Each agent (`ReviewAgent.run` in
> `app/agents/base.py`) also gathers its own chunks concurrently. A single shared
> `asyncio.Semaphore` (`LLM_MAX_CONCURRENCY`, created in `build_state`) caps how many LLM calls are
> in flight across all agents and all reviews. Everything is asyncio on one event loop: GitHub and
> Gemini go through `httpx.AsyncClient`, and SQLite goes through `asyncio.to_thread`.

### Tradeoffs

- **asyncio vs threads/processes.** The work is I/O-bound (waiting on Gemini and GitHub), so
  coroutines are cheap and simple. CPU-heavy work (chunking, rendering) is small. If it weren't,
  it would block the loop.
- **BackgroundTasks vs a real queue (Celery/RQ/SQS).** BackgroundTasks needs no extra
  infrastructure, but work dies with the process. I mitigated this: rows left `running` are
  marked `failed` at startup, and a GitHub redelivery re-runs them. At scale I'd move to a queue
  with retries and visibility timeouts.
- **One global semaphore vs a per-review limit.** Global protects the provider's rate limit no
  matter how many PRs arrive at once. The cost is that one huge PR can delay others (no fairness).
- **Per-file chunks × 4 agents = many calls.** That gives precise attribution and small prompts,
  at the cost of more requests. Packing small files together is an obvious optimisation.

### Likely questions

1. **Why return 202 instead of doing the review in the request?**
   GitHub times out deliveries after 10 seconds and then marks them failed. A review takes tens of
   seconds. The original code even fetched the diff synchronously inside the request. Now the
   request does only constant-time work (HMAC, one `INSERT`) and the rest is backgrounded.
   `test_handled_actions_return_202` and the e2e tests cover this.

2. **How do you know the agents actually run in parallel?**
   `MockLLMClient` tracks `max_in_flight`. `test_semaphore_bounds_concurrent_llm_calls` runs
   4 agents × 3 chunks with latency and a semaphore of 2, and asserts the peak is exactly 2. The
   demo shows 12 calls with a peak of 4. Wall time is about 3 × the per-call latency, not 12 ×.

3. **Why is the timeout inside the semaphore and not around it?**
   If `wait_for` wrapped the acquire, a call queued behind slow ones could "time out" without the
   model ever being asked. Measuring only the actual LLM call means a timeout really means the
   model was slow.

4. **What happens with two pushes to the same PR in quick succession?**
   Two `synchronize` deliveries have different delivery IDs, so both get reviewed. Before posting,
   `ReviewPipeline._is_superseded` re-fetches the PR's head SHA. If it moved, the older review is
   stored as `superseded` and not posted, so only the newest commit gets a comment
   (`test_superseded_head_is_not_posted`).

5. **How would you scale this to thousands of PRs per hour?**
   Move the review out of the web process onto a queue (the webhook enqueues after claiming the
   delivery). Run N workers, each with its own concurrency limit. Move SQLite to Postgres.
   Replace the in-process semaphore with a distributed rate limiter (for example a Redis token
   bucket). The pipeline code itself wouldn't change much, because `ReviewPipeline.run(review_id,
   ctx)` is already the unit of work.

---

## 2. "Per-agent fault isolation so one failure degrades gracefully"

### 30-second version

> There are two layers. In `ReviewAgent.review_chunk` (`app/agents/base.py`) every LLM call runs
> under `asyncio.wait_for` with a per-agent timeout. The response goes through
> `parse_review_response` (JSON repair plus Pydantic). On a timeout, a provider error, or invalid
> JSON it retries once. After invalid JSON, the retry adds a "reply with ONLY the JSON" hint. If
> the retry also fails, the agent doesn't raise: it returns an `AgentRun` with
> `status=degraded`, a grouped error message, and any findings from its other chunks. The second
> layer is `AgentOrchestrator.run`, which uses `gather(return_exceptions=True)`, so even an
> unexpected crash inside an agent becomes a degraded run instead of cancelling the siblings. The
> aggregator then posts the review anyway, with a "Partial review" banner and a "Degraded agents"
> table, and `agent_runs` records status, latency, attempts, error and tokens.

### Tradeoffs

- **Retry once, not N times.** The spec asks for one retry. More retries inflate tail latency and
  cost, and LLM failures (bad JSON, refusals) often repeat. Non-retryable errors (401 bad key,
  400, blocked prompt) skip the retry entirely: `LLMError.retryable=False`.
- **Partial results vs all-or-nothing.** A degraded agent keeps findings from the chunks that
  succeeded. Reviewers get more signal, but they must read the banner to know the coverage is
  partial.
- **Dropping invalid findings individually.** One malformed item doesn't throw away nine good
  ones. But if *every* item is invalid, the response is rejected, because that usually means the
  model ignored the schema.
- **Posting despite degradation.** The posted review says what's missing. The alternative, failing
  the whole review, punishes the PR author for a provider outage.

### Likely questions

1. **What's the difference between `return_exceptions=True` and catching exceptions inside each
   agent? Why both?**
   Without `return_exceptions`, the first exception propagates out of `gather` and the result of
   every sibling is lost (they keep running, but you can't collect their results). Catching
   inside the agent handles the *expected* failures with good error messages and partial results.
   `return_exceptions=True` is the safety net for *unexpected* bugs.
   `test_unexpected_crash_inside_agent_run_is_contained` monkeypatches `agent.run` to raise and
   asserts the other three still return findings.

2. **Show me the test for "one fails, one times out, one returns garbage".**
   `tests/test_fault_isolation.py::test_three_faulty_agents_do_not_affect_the_fourth` injects
   `error`, `timeout` and `garbage` faults into the mock for security, performance and
   documentation. It asserts the tests agent is OK with findings, the other three are degraded
   with the right reasons, each faulty chunk was attempted exactly twice, and the whole run is
   time-bounded. `test_degraded_agents_are_reported_in_the_posted_review` does the same through a
   signed webhook and checks the Markdown posted to (mocked) GitHub.

3. **How do you handle the model returning JSON in a code fence or with prose around it?**
   `app/llm/json_repair.py::extract_json` strips fences, finds the first *balanced* `{...}` or
   `[...]` (it respects strings and escapes, so a `}` inside a string doesn't end the object),
   removes trailing commas, and wraps a bare list as `{"findings": [...]}`. Then Pydantic
   validates it. The `Finding` validators normalise common sloppiness: `"12-15"` becomes 12,
   `"warning"` becomes medium. I also ask Gemini for `responseMimeType: application/json`, so the
   repair step is a second line of defence.

4. **Does a cancelled or timed-out call keep running in the background?**
   No. `wait_for` cancels the inner coroutine, and httpx closes that request. `CancelledError` is
   a `BaseException`, so my `except Exception` blocks don't swallow it: cancelling the review
   itself still propagates.

5. **What if the whole review crashes, for example the DB is down?**
   `ReviewPipeline.run` wraps everything and never raises out of the background task. Fetch and
   agent errors mark the review `failed` with the error. If even that write fails, the row stays
   `running` and is set to `failed` at the next startup. A `failed` delivery is the only kind that
   a GitHub redelivery is allowed to re-run (`ReviewRepository.claim_delivery`).

---

## 3. "Webhooks secured with HMAC-SHA256 signature verification"

### 30-second version

> GitHub signs every delivery with `X-Hub-Signature-256: sha256=<hex HMAC-SHA256(secret, body)>`.
> In `app/github/webhook.py`, `verify_signature` recomputes it over the **raw request bytes**
> (`await request.body()`, never re-serialised JSON) and compares the two with
> `hmac.compare_digest`, which runs in constant time. In `app/main.py` this is the first thing the
> handler does, before reading the event header or parsing JSON. A missing, malformed or wrong
> signature, or a tampered body, returns 401. If no secret is configured, it **fails closed**
> with 503. Replays and redeliveries are handled by idempotency: `X-GitHub-Delivery` is a `UNIQUE`
> column, and `claim_delivery` lets SQLite decide atomically, so duplicates never produce a
> second review.

### Tradeoffs

- **The raw body forces you to avoid framework conveniences.** Declaring a Pydantic body model
  in FastAPI would parse first, and you'd lose the exact bytes. I read `request.body()` and call
  `json.loads` only after verifying.
- **Fail closed vs fail open.** The original code skipped verification without a secret. That is
  convenient in development and catastrophic in production. Now a misconfigured server rejects
  everything and logs an error at startup.
- **HMAC proves the sender, not freshness.** GitHub doesn't sign a timestamp, so a captured
  delivery could be replayed. Delivery-ID idempotency means a replay can't create a second
  review. A replay with a new ID would need the secret to sign.
- **Secret rotation.** It supports one secret. Rotating without downtime would mean accepting
  either of two secrets for a window. That's an easy extension but not built.

### Likely questions

1. **Why `hmac.compare_digest` and not `==`?**
   `==` on strings can return as soon as the first byte differs, so response timing leaks how
   many leading bytes of a forged signature are correct. Over many requests, an attacker could
   build a valid signature byte by byte. `compare_digest` takes the same time regardless of where
   the mismatch is.

2. **Why must you verify the raw bytes?**
   The HMAC is over the exact bytes GitHub sent. `json.dumps(json.loads(body))` can change
   whitespace, key order or unicode escaping, so a correct signature would fail. Worse, verifying
   a re-encoded object means you're trusting a parser before authenticating.
   `test_reserialised_json_does_not_verify` demonstrates the mismatch.

3. **What does an unauthenticated caller learn?**
   Nothing. Even `issues` or `ping` events get 401 without a valid signature
   (`test_signature_checked_before_event_filtering`). Only after authentication do we return
   200-ignored for events we don't handle.

4. **How do you handle the same webhook being delivered twice at the same time?**
   The handler `INSERT`s a row keyed by `X-GitHub-Delivery` with a `UNIQUE` constraint. The DB
   guarantees exactly one insert wins, and the losers get an `IntegrityError` and return
   `200 duplicate`. `test_concurrent_duplicate_deliveries` fires 8 identical deliveries in
   parallel threads and asserts one 202, seven 200s, and one posted review. A failed review can
   be re-claimed by redelivery through an atomic `UPDATE ... WHERE status='failed'`.

5. **What else did you lock down around the webhook?**
   A 413 for bodies over GitHub's 25 MB cap (checked before buffering when Content-Length is
   present). An optional `API_TOKEN` for the read API, because reviews describe private code.
   `@mentions` in model output are neutralised so prompt injection can't make the bot ping people.
   Secrets are `SecretStr`, so they are masked in logs. The Gemini key goes in a header, not the
   URL. The Docker image runs as a non-root user and `.dockerignore` keeps `.env` out of it.

---

## 4. "Large diffs chunked per file to stay within model context limits"

### 30-second version

> `app/chunking.py::plan_chunks` takes the PR's changed files (from the paginated
> `/pulls/{n}/files` API). First `skip_reason` drops lockfiles, binaries, minified or vendored
> files and deletions; each skipped file is listed in the review. Each remaining file becomes one
> chunk. Every diff line is prefixed with its new-file line number (`annotate_hunk`), so the model
> can cite exact lines. If a file exceeds the budget (`CHUNK_MAX_TOKENS × CHARS_PER_TOKEN`,
> default 24,000 characters), `chunk_file` splits it on **hunk boundaries** and greedily packs
> hunks. A single hunk that is still too large is split **on line boundaries**
> (`_split_oversized_hunk`), repeating its header and keeping the original line numbers.
> `MAX_CHUNKS_PER_REVIEW` caps cost, and anything past the cap is reported as skipped rather than
> silently truncated, which is what the original code did.

### Tradeoffs

- **Estimated tokens (characters ÷ 4) vs a real tokenizer.** No dependency and no API call, but
  it's approximate. Code tokenises denser than prose, so the default budget is far below the
  model's context and leaves headroom. Actual token counts from Gemini's `usageMetadata` are
  recorded when available.
- **Per-file context only.** The model sees one file's diff (plus the list of other changed
  files), not the whole repository. Cross-file issues, such as a renamed function still called
  elsewhere, can be missed. That is the price of bounded prompts.
- **Hunk-level splits lose surrounding context.** Splitting by line can cut a function in half.
  The repeated header and the line numbers help, but quality is better when hunks fit whole.
- **No batching of small files.** Simple and precise, but many tiny files means many calls.

### Likely questions

1. **Why not just send the whole diff? Gemini has a huge context window.**
   Cost and latency scale with input. Output quality drops on very long prompts ("lost in the
   middle"). One bad file shouldn't sink the whole review. Per-file chunks also make every finding
   attributable to one path, so `_normalise` can pin hallucinated paths back to the real file.
   And the system works with smaller-context models.

2. **How do you get accurate line numbers?**
   LLMs are bad at arithmetic on `@@ -a,b +c,d @@` headers. `annotate_hunk` precomputes the
   new-file line number for every added and context line (removed lines get none), so the model
   just copies the number. `test_security_findings_point_at_the_right_lines` checks this, for
   example that the `API_KEY` finding lands on `app/auth.py:7`.

3. **What does GitHub do with huge files, and how do you handle it?**
   The files API omits `patch` for very large text files. `ReviewPipeline.fetch_files` detects
   reviewable files without a patch and fills them in from the full `.diff` endpoint, parsed by
   `parse_unified_diff`. If GitHub refuses the full diff (HTTP 406 for huge PRs), those files are
   skipped and listed as "no textual diff". The diff parser tracks hunk line counts, so a removed
   SQL comment like `--- foo` isn't mistaken for a file header (there's a test for exactly that).

4. **How are pagination and rate limits handled while fetching files?**
   `GitHubClient.paginate` follows `Link: rel="next"` with `per_page=100`, up to GitHub's
   3,000-file cap. `GitHubClient.request` retries GETs on 429, 5xx, rate-limit 403s and transport
   errors. It honours `Retry-After` and `X-RateLimit-Reset`, otherwise uses exponential backoff
   with jitter, and fails fast if GitHub asks for more than 60 s. POSTs are only retried when
   GitHub definitely didn't process them (rate-limited, or a connect error), so a 502 can't lead
   to a duplicate review.

5. **How did you test the oversized cases?**
   `tests/test_chunking.py`. A file with 6 hunks under a 2-hunk budget yields exactly 3 chunks,
   with no hunk lost or duplicated. A single 200-line hunk under a 2,000-character budget is split
   by lines, every piece fits, all 200 lines are present, and the first and last line numbers are
   preserved. A single 10,000-character line is truncated with a marker rather than dropped. A
   giant hunk header can't blow the budget. The chunk cap reports the excess files as skipped.

---

## Quick facts to have ready

- Stack: Python 3.11+, FastAPI, httpx (async), Pydantic v2 + pydantic-settings, SQLite (stdlib),
  pytest + pytest-asyncio, ruff, Docker, GitHub Actions.
- Tests: 231, fully offline (GitHub via `httpx.MockTransport`, LLM via `MockLLMClient`), 97% line
  coverage of `app/` (CI gate: 85%).
- Gemini is called over REST (`:generateContent`) with httpx, not an SDK. The old
  `google-generativeai` package is deprecated, and REST is natively async and easy to mock.
- What I'd do next: inline review comments on specific lines, a job queue, batching small files,
  cross-file context (for example a repo map), secret rotation, and per-repo configuration.
- Be honest: the offline demo uses a regex-based mock, not a real model. Review quality with real
  Gemini depends on the prompts in `app/agents/prompts/`.
