# Jira AI Agent

An autonomous agent that watches a Jira ticket, detects when it moves to **"In Dev"**, automatically creates a feature branch, generates code changes using **GitHub Copilot API (Claude Sonnet)**, opens a Pull Request on Bitbucket, and transitions the ticket to **"Unit Testing"** — all with zero human intervention.

---

## 🚀 How to Run

### 1. Clone & install dependencies

```bash
git clone <this-repo>
cd jira-ai-agent
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure `.env`

Copy the sample and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env` with your values (see [Configuration](#configuration) below).

### 3. Run the autonomous watcher (main use case)

Watch a single Jira ticket and auto-trigger on status → **"In Dev"**:

```bash
source .venv/bin/activate
python watcher.py BLITZ-12442
```

With custom poll interval (default is 15 seconds):

```bash
python watcher.py BLITZ-12442 --interval 30
```

With a custom trigger status:

```bash
python watcher.py BLITZ-12442 --status "Ready for Dev"
```

### 4. Run the FastAPI webhook server (for live Jira webhooks)

```bash
source .venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Health check:
```bash
curl http://localhost:8000/health
```

---

## ✅ What the Watcher Does (Full Pipeline)

When a watched ticket transitions to **"In Dev"**, the watcher automatically:

| Step | Action |
|------|--------|
| 1 | Fetches Jira ticket details (summary, description, type, priority) |
| 2 | Creates branch `featureAutoGen/<TICKET-KEY>` from `release/dev` |
| 3 | Pushes the branch to Bitbucket |
| 4 | Sends source files + ticket requirements to **GitHub Copilot API (claude-sonnet-4.6)** |
| 5 | Applies AI-generated code changes, commits and pushes |
| 6 | Opens a Pull Request on Bitbucket with full Jira description |
| 7 | Posts a comment on the Jira ticket with branch + PR link |
| 8 | **Auto-transitions the ticket to "Unit Testing"** |

All activity is logged to `watcher.log`.

---

## Configuration

All configuration lives in `.env`. Create it from `.env.example`:

| Variable | Description |
|----------|-------------|
| `JIRA_URL` | Your Jira instance URL, e.g. `https://your-org.atlassian.net` |
| `JIRA_USER` | Jira account email |
| `JIRA_TOKEN` | Atlassian API token — generate at https://id.atlassian.com/manage-profile/security/api-tokens |
| `JIRA_WEBHOOK_SECRET` | Optional — for webhook signature verification |
| `BB_USERNAME` | Bitbucket username |
| `BB_API_TOKEN` | Bitbucket API token — generate at https://bitbucket.org/account/settings/api-tokens |
| `BB_WORKSPACE` | Bitbucket workspace slug |
| `BB_REPO_SLUG` | Bitbucket repository slug |
| `BB_LOCAL_REPO` | Absolute path to local clone of the Bitbucket repo |
| `BB_BASE_BRANCH` | Base branch for PRs, e.g. `release/dev` |
| `COPILOT_OAUTH_TOKEN` | GitHub Copilot OAuth token — run `python get_copilot_token.py` to obtain |
| `COPILOT_MODEL` | Copilot model to use (default: `claude-sonnet-4.6`) |
| `COPILOT_BASE_URL` | Copilot API base URL (default: `https://api.githubcopilot.com`) |

---

## Project Structure

```
jira-ai-agent/
├── main.py                  # FastAPI app entry point (webhook server)
├── watcher.py               # Autonomous Jira watcher daemon
├── get_copilot_token.py     # One-time script to obtain GitHub Copilot OAuth token
├── watcher_state.json       # Persisted state (tracks processed tickets)
├── watcher.log              # Runtime log output
├── requirements.txt
├── .env                     # Your credentials (never commit this)
├── .env.example
└── app/
    ├── jira/
    │   ├── client.py        # Jira REST API wrapper
    │   └── webhook_handler.py  # FastAPI route for Jira webhooks
    ├── git/
    │   ├── branch_manager.py   # Creates and pushes feature branches
    │   ├── code_applier.py     # Writes AI code changes and commits
    │   └── pr_creator.py       # Opens PRs via GitHub API
    ├── ai/
    │   ├── code_generator.py   # OpenAI/LLM wrapper
    │   ├── context_builder.py  # Collects relevant source files
    │   └── prompt_builder.py   # Builds prompts for code gen + PR description
    ├── workers/
    │   └── job_processor.py    # Full pipeline orchestrator
    └── config/
        └── settings.py         # Pydantic settings (reads .env)
```

---

## Re-running a Ticket

The watcher tracks processed tickets in `watcher_state.json` to avoid duplicate runs. To reprocess a ticket:

```bash
echo '{"processed":{}}' > watcher_state.json
python watcher.py BLITZ-12442 --interval 15
```

---

## Logs

All watcher activity is written to both **stdout** and `watcher.log`:

```bash
tail -f watcher.log
```

---

## Requirements

- Python 3.10+
- A local clone of the target Bitbucket repository
- Jira account with API token
- Bitbucket account with API token (Repositories + Pull requests: Read/Write)
- GitHub Copilot subscription with OAuth token (run `python get_copilot_token.py` once to generate)
