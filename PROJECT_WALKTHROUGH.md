# ReviewMind: the project in my own words

This is written in the first person so I can rehearse it out loud. Everything here matches the
code on `upgrade/production-ready`. Where something has only been tested offline, it says so.
Before an interview, I should run `scripts/live_smoke.py` against a real PR and add the real
results here.

---

## Part 1: The elevator pitch (30 seconds)

> ReviewMind is a GitHub bot that reviews pull requests with LLMs. When someone opens or pushes to
> a PR, GitHub sends a signed webhook to my FastAPI service. I verify the HMAC signature, return
> 202 straight away, and in the background I fetch the changed files, split them into per-file
> chunks, and run four specialist agents in parallel: security, performance, documentation and
> tests. Each agent is an LLM prompt with its own focus, and each returns validated JSON findings.
> If one agent fails or times out, the others still finish, and the review that gets posted says
> which agent degraded. Findings are merged, deduplicated, sorted by severity and posted back to
> the PR as one Markdown review. Everything is stored in SQLite so there's an API to look at past
> reviews.

---

## Part 2: Why I built it

Code review is slow, and reviewers miss things in big PRs: a hard-coded key, an N+1 query, a new
function with no tests. One general "review this diff" prompt tends to give shallow, mixed
feedback. I wanted to see if **specialised agents** (one focused job each, run in parallel) give
more useful reviews, and I wanted to build it the way a real service would be built: secure,
fault-tolerant, testable.

---

## Part 3: How I built it, step by step

### Step 0: The prototype, then an honest audit

My first version worked as a demo, but when I audited it (that's `AUDIT.md`), I found real
problems:

- If the webhook secret wasn't set, it **accepted unsigned requests** (fail-open).
- It fetched the diff **synchronously inside the request**, so GitHub's 10-second webhook timeout
  could trigger redeliveries, and with no idempotency that meant **duplicate reviews**.
- "Chunking" was actually **truncation**: anything after 16,000 characters was silently dropped.
- A failed agent showed "✅ No issues found", which is a silent false negative.
- No tests at all, and the `.gitignore` was saved as UTF-16, so git ignored nothing.

So I rebuilt it into a proper package: `app/`, `app/agents/`, `app/llm/`, `app/github/`,
`tests/`.

### Step 1: The webhook endpoint (`app/main.py`, `app/github/webhook.py`)

The flow for `POST /webhook/github`:

1. If no secret is configured, return **503**: fail closed.
2. Read the **raw bytes** with `await request.body()`.
3. `verify_signature()` computes `sha256=` + HMAC-SHA256(secret, raw bytes) and compares it
   with the `X-Hub-Signature-256` header using **`hmac.compare_digest`**. A wrong, missing or
   tampered signature returns **401**. This happens *before* I look at anything else, so an
   attacker learns nothing.
4. `ping` returns 200 "pong". Other event types return 200 "ignored". For `pull_request`, I only
   handle `opened`, `synchronize` and `reopened`; other actions return 200 "ignored".
5. Parse the payload into a `PRContext` (repo, PR number, head SHA, and so on).
6. **Idempotency:** `claim_delivery()` inserts a row keyed by `X-GitHub-Delivery`, which has a
   `UNIQUE` constraint. If the insert fails, it's a duplicate, so I return 200 "duplicate" and do
   nothing.
7. Schedule `ReviewPipeline.run` as a background task and return **202**.

The request does only constant-time work, so it always answers well inside GitHub's timeout.

### Step 2: Fetching the PR (`app/github/client.py`)

I wrote a small async GitHub client on **httpx**:

- `list_pull_request_files()` uses the files API with `per_page=100` and follows the `Link:
  rel="next"` header for **pagination**.
- For very large files GitHub leaves out the `patch` field, so I fall back to the full `.diff`
  and parse it myself (`parse_unified_diff`).
- **Retries:** GET requests retry on 429, 5xx, rate-limit 403s and network errors, using
  exponential backoff with jitter. I honour `Retry-After` and `X-RateLimit-Reset`. If GitHub
  asks me to wait longer than 60 seconds, I fail fast instead of blocking for an hour.
- **POST requests are only retried when I know GitHub didn't process them** (rate-limited, or the
  connection never opened). If I retried after a 502, I might post the review twice.

### Step 3: Chunking (`app/chunking.py`)

Big diffs don't fit well in one prompt, and long prompts make models worse. So `plan_chunks()`:

1. **Skips** lockfiles, binaries, minified and vendored files, and deleted files. These are
   listed in the review as skipped, with the reason.
2. Makes **one chunk per file**.
3. If a file is over budget (`CHUNK_MAX_TOKENS × CHARS_PER_TOKEN`, about 24,000 characters by
   default), it **splits on hunk boundaries** and packs hunks greedily.
4. If a single hunk is still too big, it **splits on line boundaries** and repeats the hunk
   header.
5. Caps the total at `MAX_CHUNKS_PER_REVIEW`. Extra files are reported as skipped, never silently
   dropped.

One detail I'm proud of: I **prefix every diff line with its line number in the new file**
(`annotate_hunk`). LLMs are bad at working out line numbers from `@@ -10,3 +20,4 @@` headers, so
I do the maths and the model just copies the number. That makes the line numbers in findings
reliable.

### Step 4: The LLM layer (`app/llm/`)

- `LLMClient` is an abstract interface with one method, `complete(request)`.
- `GeminiClient` calls Gemini's REST `generateContent` endpoint directly with httpx. I didn't use
  the SDK because the old one is deprecated, and plain HTTP is natively async and easy to mock.
  The API key goes in a header, never in the URL, so it can't leak into logs.
- `MockLLMClient` is a deterministic, regex-based fake reviewer. It lets the whole system run
  and be tested **without network access or API keys**. It also supports fault injection (`error`,
  `timeout`, `garbage`, `flaky`), which is how I test fault isolation.
- `LLM_PROVIDER=gemini|mock` selects the client. The default is `gemini`, and startup fails
  without a key. I deliberately didn't default to mock, because then a misconfigured production
  server could post fake reviews.
- `json_repair.py`: models sometimes wrap JSON in code fences, add prose, or leave trailing
  commas. `extract_json()` handles all of that (including braces inside strings), then
  **Pydantic** validates every finding: `file`, `line`, `severity`, `title`, `explanation`,
  `suggestion`.

### Step 5: The agents (`app/agents/`)

Each agent is a small subclass of `ReviewAgent` with its own system prompt in
`app/agents/prompts/`. One shared output contract is appended to all four prompts, so the schema
is defined once.

`ReviewAgent.review_chunk()` is where the reliability lives:

1. Acquire the **shared semaphore**.
2. Call the LLM inside **`asyncio.wait_for(timeout)`**. The timeout is *inside* the semaphore,
   so time spent waiting in the queue doesn't count as the model being slow.
3. Parse and validate the JSON.
4. On a timeout, an LLM error or bad JSON, **retry once**. After bad JSON, the retry adds "reply
   with ONLY the JSON". Errors that can't be fixed by retrying (like a 401 bad API key) skip the
   retry.
5. If the retry also fails, mark that chunk failed. **The agent never raises**: it returns an
   `AgentRun` with `status=degraded`, the error, and any findings from its other chunks.

### Step 6: Running agents in parallel (`app/orchestrator.py`)

`AgentOrchestrator.run()` starts all four agents with
**`asyncio.gather(..., return_exceptions=True)`**. That's my second layer of fault isolation:
even if a bug escapes an agent's own error handling, it becomes a "degraded" result instead of
cancelling the other three agents' results.

A single `asyncio.Semaphore(LLM_MAX_CONCURRENCY)` (default 4) is shared by **every agent and
every concurrent review**, so the total number of in-flight LLM calls is always bounded,
whatever the traffic.

### Step 7: Aggregation (`app/aggregator.py`)

- **Merge and dedupe:** two findings are duplicates if they're on the same file and line and
  their titles are the same or very similar (`difflib` ratio ≥ 0.82). The merged finding keeps
  the higher severity and notes "also flagged by…".
- **Sort** by severity: critical, high, medium, low, info.
- **Render Markdown:** a summary line, a table per agent (status, counts, latency), a section
  per agent with low-severity items collapsed, a **"Partial review" banner and a "Degraded
  agents" table** if anything failed, and the list of skipped files.
- **Safety:** I neutralise `@mentions` in model output (so prompt injection can't make the bot
  ping people), and shrink the list if the body would exceed GitHub's 65,536-character limit.

### Step 8: Posting and persistence (`app/pipeline.py`, `app/db.py`)

- Before posting, I **re-check the PR's head SHA**. If someone pushed new commits during the
  review, I mark this review `superseded` and don't post it, because the newer push gets its own
  review.
- I post it as a PR **review** with `event: COMMENT`. If GitHub rejects it with a 422, I fall
  back to a normal comment.
- SQLite has three tables: `reviews`, `agent_runs` (status, latency, error, attempts, token
  estimate), and `findings`. I use the standard `sqlite3` module through `asyncio.to_thread` so
  it doesn't block the event loop, with a fresh connection per operation and WAL mode.
- **Crash recovery:** on startup, any review stuck in `queued` or `running` is marked `failed`.
  A failed review is the only kind that a GitHub "Redeliver" is allowed to re-run.
- Read API: `GET /health`, `GET /reviews`, `GET /reviews/{id}`, optionally protected by a bearer
  `API_TOKEN`.

### Step 9: Ops

- **Pydantic Settings** for every config value, with secrets as `SecretStr` so they're masked
  in logs.
- **Structured JSON logs.** Context variables carry `delivery_id`, `review_id`, `repo` and `pr`
  into every log line.
- **Docker:** slim image, non-root user, healthcheck, data volume, and a `.dockerignore` so `.env`
  never gets baked into the image. Plus `docker-compose.yml`.
- **GitHub Actions CI:** ruff lint and format, pytest on Python 3.11/3.12/3.13 with an 85%
  coverage gate, and a Docker build. It's green.

### Step 10: Testing

231 tests, **fully offline**: GitHub is mocked with `httpx.MockTransport` and the LLM with
`MockLLMClient`. Coverage is about 97% of `app/`. The ones I'd mention:

- Signatures: valid, invalid, missing, tampered body, and re-serialised JSON (which proves why
  raw bytes matter).
- Fault isolation: one agent raises, one times out, one returns garbage. The fourth still
  succeeds, and the posted review lists the degraded ones.
- Idempotency: 8 identical deliveries fired **concurrently** produce exactly one review.
- Chunking: oversized files and oversized hunks, checking that every line survives and line
  numbers are preserved.
- End to end: a signed webhook plus a sample diff produces the exact Markdown posted to (mocked)
  GitHub.

I also wrote `scripts/demo.py`, which replays a signed webhook through the real app and prints
the review. `scripts/live_smoke.py` tests against the real Gemini and GitHub APIs.

---

## Part 4: Tech stack in one breath

Python 3.11+, FastAPI, asyncio, httpx, Pydantic v2, pydantic-settings, SQLite, Gemini 2.0 Flash
over REST, pytest and pytest-asyncio, ruff, Docker, GitHub Actions.

---

## Part 5: Interview questions and my answers

### A. Big picture

**Q1. Walk me through what happens when a PR is opened.**
GitHub sends a `pull_request` webhook to `/webhook/github`. I verify the HMAC signature over the
raw body, check that it's an action I handle, and claim the delivery ID in SQLite for
idempotency. Then I return 202 and start a background task. That task fetches the changed files
with pagination, chunks them per file, and runs four agents in parallel under a shared semaphore.
Each agent validates its JSON and retries once on failure. I merge, dedupe and sort the findings,
render Markdown, check the PR head hasn't moved, post the review, and save everything to SQLite.

**Q2. Why four agents instead of one big prompt?**
Focus. A single prompt asked to find security, performance, documentation and testing issues at
once tends to give shallow coverage of each. Specialised prompts are easier to tune
individually, they run in parallel so latency doesn't add up, and if one fails I only lose that
dimension, not the whole review. The cost is about four times the input tokens, which I control
with chunking and a concurrency cap.

**Q3. What was the hardest part?**
Making failure behaviour correct, not just making the happy path work. LLMs time out, return
broken JSON, or get rate-limited, and GitHub retries webhooks. I had to think carefully about
where timeouts go (inside the semaphore), what to retry (not 401s, and not POSTs that might have
gone through), and how to make duplicate deliveries safe under concurrency. That's why I let the
database's `UNIQUE` constraint decide, instead of checking-then-inserting in Python.

**Q4. What would you do differently or next?**
Inline comments on the exact diff lines (findings already have file and line). A real job queue
instead of in-process background tasks, so it scales across instances. Batching small files into
one LLM call to save cost. And giving the model some cross-file context, because right now it
only sees one file at a time.

### B. Concurrency and Python

**Q5. Why asyncio and not threads?**
The work is almost entirely waiting on network calls to Gemini and GitHub. Coroutines handle
that cheaply on one thread. The only blocking library I use is `sqlite3`, and I push it to a
thread with `asyncio.to_thread`.

**Q6. What does `return_exceptions=True` actually change?**
Without it, `gather` raises the first exception, and I lose the results of the other tasks.
With it, exceptions come back as values in the result list, so I can turn a crashed agent into a
"degraded" result and still use the other three. I have a test that makes one agent's `run()`
raise and checks the others still return findings.

**Q7. Why is the timeout inside the semaphore?**
If I wrapped the acquire in the timeout, a call stuck waiting in the queue behind slow calls could
"time out" without ever reaching the model. Putting `wait_for` inside means a timeout really means
the model was slow.

**Q8. How do you know the agents actually run in parallel?**
My mock LLM tracks the peak number of in-flight calls. One test runs 12 calls with a semaphore
of 2 and asserts the peak is exactly 2. In the demo, 12 calls with a limit of 4 finish in about 3
call-lengths instead of 12.

**Q9. What happens when a background task is cancelled or the server crashes mid-review?**
Cancellation propagates, because I catch `Exception`, not `BaseException`, so I don't swallow
`CancelledError`. If the process dies, the review row is left in `running`. At the next startup I
mark those as `failed`, and GitHub's "Redeliver" button can then re-run it.

### C. Security

**Q10. Explain HMAC webhook verification.**
GitHub and I share a secret. GitHub sends `sha256=` followed by the HMAC-SHA256 of the body with
that secret. I recompute it over the exact raw bytes and compare with `hmac.compare_digest`. If
they match, the request came from someone who knows the secret and the body wasn't modified.

**Q11. Why `compare_digest` instead of `==`?**
`==` can stop at the first different character, so the response time leaks how much of a forged
signature is correct, and an attacker could build a valid one byte by byte. `compare_digest`
takes the same time no matter where the difference is.

**Q12. Why must you use the raw body?**
The signature is over the exact bytes GitHub sent. If I parse the JSON and re-serialise it,
whitespace, key order or escaping can change, and a valid signature would fail. Also, verifying
after parsing means trusting the parser with unauthenticated input. I have a test that shows
re-serialised JSON fails verification.

**Q13. What if the secret isn't configured?**
It fails closed: every webhook gets 503 and an error is logged at startup. My first version
skipped verification in that case, which is exactly the kind of bug that ends up in production.

**Q14. How do you stop replays or duplicate deliveries?**
Every delivery has a unique `X-GitHub-Delivery` ID, stored in a `UNIQUE` column. A second
delivery with the same ID fails the insert and is ignored, even if eight arrive at the same time
(I test that). HMAC alone doesn't stop replays, since GitHub doesn't sign a timestamp, but
idempotency means a replay can't produce a second review.

**Q15. What about prompt injection? The diff is attacker-controlled.**
I treat model output as untrusted. The system prompt tells the model the diff is untrusted input.
The only thing model output can do is become Markdown in a review body. I neutralise `@mentions`
so it can't ping people, and I validate the structure with Pydantic. I wouldn't claim that stops
a model from being influenced. It limits what an influenced model can *do*.

### D. Chunking and LLM engineering

**Q16. Why chunk if Gemini has a huge context window?**
Cost and latency grow with input, and quality drops on very long prompts. Per-file chunks also
mean each finding maps to exactly one file. If the model invents a path, I just pin it back to
the chunk's file.

**Q17. How do you count tokens?**
I estimate: characters divided by 4. It's approximate, so the default budget is well below the
model's limit. When Gemini returns actual usage counts, I store those in `agent_runs` instead.

**Q18. How do you get accurate line numbers?**
I pre-compute the new-file line number for every line and prefix it, so the model copies the
number instead of calculating it. A test checks, for example, that the hard-coded API key is
reported at exactly `app/auth.py:7` in the sample diff.

**Q19. How do you handle bad JSON from the model?**
Three layers. I ask Gemini for `application/json` output. I repair common problems (code
fences, surrounding prose, trailing commas, a bare list). Then Pydantic validates each finding
and normalises sloppy values like `"12-15"` for a line or `"warning"` for a severity. Individual
bad findings are dropped. If everything is invalid, I retry once with a stricter instruction,
then mark the agent degraded.

**Q20. Why call Gemini over REST instead of using the SDK?**
The old Python SDK is deprecated. REST over httpx is natively async, needs no extra dependency,
and I can test it with `httpx.MockTransport` exactly like the GitHub client.

### E. Reliability and GitHub API

**Q21. How do you handle GitHub rate limits?**
I detect both the primary limit (403 or 429 with `X-RateLimit-Remaining: 0`) and the secondary
limit (`Retry-After` or the message text). I wait for exactly the time GitHub asks, or use
exponential backoff with jitter. If the wait would be more than 60 seconds, I fail fast rather
than hold the task open.

**Q22. Why not retry POSTs on 5xx?**
A 502 doesn't mean GitHub didn't create the review. It might have, and then the proxy failed.
Retrying could post the review twice. I only retry POSTs when I know they weren't processed: a
rate-limit response, or a connection that never opened.

**Q23. Two pushes in quick succession: what happens?**
Each push is its own delivery, so both are reviewed. Before posting, I check the current head
SHA. The older review sees the head moved, so it's saved as `superseded` and not posted. Only the
latest commit gets a comment.

### F. Testing and quality

**Q24. How did you test something that depends on an LLM and GitHub?**
I designed for it from the start. The LLM sits behind an interface, so tests use a
deterministic mock with fault injection. GitHub is an `httpx.MockTransport` that serves a
sample diff, paginates, and records what I post. That gives 231 tests, no network, about 4
seconds, 97% coverage. The end-to-end test sends a properly signed webhook and asserts the exact
Markdown that would be posted.

**Q25. Did the tests find real bugs?**
Yes. For example, very long hunk headers could push a chunk over budget. My test helper was
skipping validation, so secrets stayed plain strings and an auth check broke. And the degraded
error message repeated the same error for every chunk and included raw model output in a public
comment. I fixed each one and kept the test.

**Q26. Have you run it against real Gemini?**
*(Answer honestly. Before the interview, run `python scripts/live_smoke.py review --repo … --pr
…` and put the real result here: how long it took, how many findings, anything surprising.)*

### G. Behavioural

**Q27. Tell me about a time you found a problem in your own work.**
When I audited my first version, I realised my CV-level claims weren't fully true. "Chunked per
file" was really truncation, and "fault isolation" showed failures as "no issues found". Rather
than leave it, I wrote an audit, listed every gap, and rebuilt it with tests that prove each
claim. That made me much more confident talking about it.

**Q28. How did you decide what *not* to build?**
I kept the scope to the spec and wrote down every judgement call in `DECISIONS.md`, for example
not adding a job queue yet and not batching small files. Each one has a reason and a note on
what would change my mind. Scope creep was the biggest risk to finishing it properly.
