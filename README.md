# ReviewMind 🤖

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green.svg)](https://fastapi.tiangolo.com)
[![Gemini](https://img.shields.io/badge/Gemini-2.0_Flash-orange.svg)](https://ai.google.dev)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A multi-agent AI code review system that automatically analyses GitHub Pull Requests
using 4 specialised Gemini 2.0 Flash agents running in parallel and posts a structured
review comment back to your PR.

---

## Architecture

```
GitHub PR (opened/synchronize)
         │
         ▼
  ┌─────────────┐
  │  POST       │   X-Hub-Signature-256
  │  /webhook   │◄──────────────────────── GitHub
  └──────┬──────┘
         │ BackgroundTask (responds <3s)
         ▼
  ┌─────────────────────────────────────────────┐
  │              Orchestrator                    │
  │                                             │
  │  diff chunked to ≤4000 tokens               │
  │                                             │
  │   asyncio.gather() ──► 4 agents in parallel │
  │                                             │
  │  ┌──────────┐ ┌───────────┐ ┌─────┐ ┌─────┐│
  │  │ Security │ │Performance│ │Docs │ │Tests││
  │  │  Agent   │ │  Agent    │ │Agent│ │Agent││
  │  └────┬─────┘ └─────┬─────┘ └──┬──┘ └──┬──┘│
  └───────┼─────────────┼──────────┼────────┼───┘
          └─────────────┴──────────┘        │
                        │                   │
                        ▼                   │
               Merge findings ◄─────────────┘
                        │
          ┌─────────────┼─────────────┐
          ▼             ▼             ▼
    Post GitHub    Save to        Return
    PR comment     SQLite         results
```

---

## Local Setup

### Prerequisites
- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- A GitHub account with a personal access token
- A free [Google AI Studio](https://aistudio.google.com) account for Gemini API key

### 1. Clone and install

```bash
git clone https://github.com/yourname/reviewmind.git
cd reviewmind

# With uv (recommended)
uv venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
uv pip install -r requirements.txt

# Or with pip
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
GEMINI_API_KEY=your_gemini_api_key_here
GITHUB_TOKEN=ghp_your_github_pat_here
GITHUB_WEBHOOK_SECRET=any_random_secret_string
PORT=8000
```

**Getting a Gemini API key (free):**
1. Go to [Google AI Studio](https://aistudio.google.com/app/apikey)
2. Click "Create API key"
3. Copy the key into `.env`

**Getting a GitHub token:**
1. GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)
2. Generate new token with scopes: `repo` (full)
3. Copy into `.env`

### 3. Run the server

```bash
uvicorn main:app --reload --port 8000
```

Verify it works:

```bash
curl http://localhost:8000/health
# {"status":"ok","version":"1.0.0"}
```

---

## Setting up the GitHub Webhook with ngrok

### Install ngrok

```bash
# macOS
brew install ngrok

# Windows
choco install ngrok
# or download from https://ngrok.com/download
```

### Expose your local server

```bash
ngrok http 8000
```

You'll see output like:
```
Forwarding  https://abc123.ngrok-free.app -> http://localhost:8000
```

### Configure GitHub Webhook

```bash
# Using GitHub CLI
gh api repos/OWNER/REPO/hooks \
  --method POST \
  --field "name=web" \
  --field "config[url]=https://abc123.ngrok-free.app/webhook" \
  --field "config[content_type]=json" \
  --field "config[secret]=your_webhook_secret" \
  --field "events[]=pull_request" \
  --field "active=true"
```

Or via the GitHub web UI:
1. Go to `https://github.com/OWNER/REPO/settings/hooks`
2. Click **Add webhook**
3. Payload URL: `https://abc123.ngrok-free.app/webhook`
4. Content type: `application/json`
5. Secret: same value as `GITHUB_WEBHOOK_SECRET` in your `.env`
6. Events: select **Pull requests** only
7. Click **Add webhook**

---

## Manual Agent Testing

Use the included `test_diff.txt` to test each agent without a real PR:

```python
import asyncio
from agents import SecurityAgent, PerformanceAgent, DocsAgent, TestsAgent

diff = open("test_diff.txt").read()
context = {"pr_title": "Add auth and reporting", "repo_full_name": "test/repo"}

async def test_all():
    for Agent in [SecurityAgent, PerformanceAgent, DocsAgent, TestsAgent]:
        result = await Agent().review(diff, context)
        print(f"\n=== {result['agent'].upper()} ===")
        print(f"Summary: {result['summary']}")
        for f in result['findings']:
            print(f"  [{f['severity'].upper()}] {f['line']}: {f['issue']}")

asyncio.run(test_all())
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `POST` | `/webhook` | GitHub webhook receiver |
| `GET` | `/reviews/{owner}/{repo}` | Last 10 reviews for a repo |
| `GET` | `/reviews/{owner}/{repo}/{pr}` | Specific PR review details |

---

## Example PR Review Comment

```
## 🤖 ReviewMind Analysis

### 🔒 Security
- 🔴 **[HIGH]** `app/auth.py:5-6` — Hardcoded credentials (DB_PASSWORD, API_KEY) in source code
  > 💡 Move to environment variables and load via os.getenv()

### ⚡ Performance
- 🔴 **[HIGH]** `app/reports.py:6-12` — N+1 query pattern: DB calls inside nested loops
  > 💡 Batch-fetch all users and orders with WHERE id IN (...) before the loop

### 📝 Documentation
- 🟡 **[MEDIUM]** `app/reports.py:29` — Magic number 1.337 with no explanation
  > 💡 Extract to a named constant: SCORE_MULTIPLIER = 1.337

### 🧪 Tests
- 🟡 **[MEDIUM]** `app/auth.py:general` — authenticate_user has no corresponding test file changes
  > 💡 Add tests for: valid login, wrong password, SQL injection attempt, empty username

---
*Reviewed by ReviewMind | 2024-01-15 14:32 UTC | 3 files changed*
```

---

## Deploy to Render (Free)

1. Push your code to a GitHub repo (make sure `.env` is in `.gitignore`)
2. Go to [render.com](https://render.com) → New → Web Service
3. Connect your GitHub repo
4. Settings:
   - **Runtime**: Python 3
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `uvicorn main:app --host 0.0.0.0 --port 8000`
5. Add environment variables in Render dashboard (same as `.env`)
6. Deploy — Render gives you a public URL like `https://reviewmind.onrender.com`
7. Update your GitHub webhook URL to the Render URL

---

## Docker

```bash
# Build
docker build -t reviewmind .

# Run
docker run -p 8000:8000 --env-file .env reviewmind
```

---

## Project Structure

```
reviewmind/
├── main.py              # FastAPI app + routes
├── webhook.py           # Signature verification + payload parsing
├── orchestrator.py      # Parallel agent coordination
├── github_client.py     # GitHub API wrapper
├── database.py          # SQLite persistence
├── models.py            # Pydantic schemas
├── config.py            # Environment config
├── agents/
│   ├── base_agent.py    # Abstract base with retry logic
│   ├── security_agent.py
│   ├── performance_agent.py
│   ├── docs_agent.py
│   └── tests_agent.py
├── prompts/             # System prompts for each agent
│   ├── security.txt
│   ├── performance.txt
│   ├── docs.txt
│   └── tests.txt
├── test_diff.txt        # Sample diff for manual testing
├── requirements.txt
├── Dockerfile
└── README.md
```
