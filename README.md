# Jira AI Agent

An autonomous agent that watches a Jira ticket, detects when it transitions to **"In Dev"**, automatically creates a feature branch, generates code changes using the **GitHub Copilot API (Claude Sonnet)**, opens a Pull Request on Bitbucket/GitHub, posts back to Jira, and transitions the ticket to **"Unit Testing"** — all with zero human intervention.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Module Reference](#module-reference)
  - [watcher.py — Autonomous Daemon](#watcherpy--autonomous-daemon)
  - [main.py — FastAPI Webhook Server](#mainpy--fastapi-webhook-server)
  - [app/jira/](#appjira)
  - [app/ai/](#appai)
  - [app/git/](#appgit)
  - [app/workers/](#appworkers)
  - [app/config/](#appconfig)
  - [get\_copilot\_token.py](#get_copilot_tokenpy)
  - [dummy\_trigger.py](#dummy_triggerpy)
- [Full Pipeline Walkthrough](#full-pipeline-walkthrough)
- [How to Run](#how-to-run)
- [Configuration Reference](#configuration-reference)
- [Safety Guarantees](#safety-guarantees)
- [Logging](#logging)
- [State Persistence](#state-persistence)
- [Requirements](#requirements)
- [Extending the Agent](#extending-the-agent)

---

## Overview

The Jira AI Agent bridges your Jira board and your source code repository. Once a ticket is moved to the trigger status (default: **"In Dev"**), the agent takes over:

1. Reads the ticket's full context (summary, description, acceptance criteria).
2. Identifies the most relevant source files in the local repository clone.
3. Calls the GitHub Copilot API to generate implementation code and unit tests.
4. Commits the changes to a new feature branch and pushes it to the remote.
5. Opens a draft Pull Request with an AI-generated description.
6. Comments on the Jira ticket with the branch name and PR link.
7. Transitions the Jira ticket to **"Unit Testing"**.

The agent **never merges PRs**. All merges require a human review.

---

## Architecture

```
┌──────────────────┐        poll / webhook        ┌───────────────────────┐
│   Jira Board     │ ──────────────────────────▶  │  watcher.py / main.py │
│  (ticket moves   │                              │  (trigger detection)   │
│   to "In Dev")   │                              └──────────┬────────────┘
└──────────────────┘                                         │
                                                  ┌──────────▼────────────┐
                                                  │  job_processor.py     │
                                                  │  (pipeline orchestr.) │
                                                  └──┬──────┬──────┬──────┘
                                                     │      │      │
                                        ┌────────────▼┐  ┌──▼──┐  ┌▼──────────────┐
                                        │ JiraClient  │  │ AI  │  │ Git / GitHub  │
                                        │ (fetch issue│  │Agent│  │ (branch, code,│
                                        │  + comment) │  │(LLM)│  │  PR, push)    │
                                        └─────────────┘  └─────┘  └───────────────┘
```

The agent supports two trigger modes:

| Mode | Description |
|------|-------------|
| **Watcher (polling)** | `watcher.py` polls a single Jira ticket on a configurable interval. Suitable for local use or CI. |
| **Webhook (push)** | `main.py` runs a FastAPI server that receives Jira webhook events in real time. Suitable for server deployment. |

---

## Project Structure

```
jira-ai-agent/
├── main.py                    # FastAPI app entry point (webhook server mode)
├── watcher.py                 # Autonomous polling daemon (primary usage)
├── dummy_trigger.py           # Manual trigger: creates branch + PR from a ticket key/URL
├── get_copilot_token.py       # One-time Device Flow auth to obtain Copilot OAuth token
├── watcher_state.json         # Persisted state to avoid reprocessing tickets
├── watcher.log                # Runtime log file (created on first run)
├── requirements.txt           # Python dependencies
├── .env                       # Your credentials — never commit
├── .env.example               # Template for .env
└── app/
    ├── __init__.py
    ├── config/
    │   ├── __init__.py
    │   └── settings.py        # Pydantic Settings — reads .env automatically
    ├── jira/
    │   ├── __init__.py
    │   ├── client.py          # Jira REST API v3 wrapper
    │   └── webhook_handler.py # FastAPI router for /webhook/jira
    ├── ai/
    │   ├── __init__.py
    │   ├── code_generator.py  # GitHub Copilot API client (OpenAI-compatible)
    │   ├── context_builder.py # Keyword-based relevant file selector
    │   └── prompt_builder.py  # System + user prompt templates
    ├── git/
    │   ├── __init__.py
    │   ├── branch_manager.py  # Creates + pushes feature branches (GitPython + PyGithub)
    │   ├── code_applier.py    # Writes AI files to disk, commits, pushes
    │   └── pr_creator.py      # Opens draft PRs via GitHub API
    └── workers/
        ├── __init__.py
        └── job_processor.py   # 4-phase pipeline orchestrator
```

---

## Module Reference

### `watcher.py` — Autonomous Daemon

The primary entry point for local/CI use. Polls a Jira ticket at a set interval and runs the full pipeline when the trigger status is detected.

**Key responsibilities:**
- Argument parsing (`argparse`): ticket key, `--interval`, `--status`
- Calls `get_ticket_status()` via Jira REST API every `POLL_INTERVAL` seconds
- Manages `watcher_state.json` to skip already-processed tickets
- Fetches the full issue (description, type, priority, URL)
- Scans the local repository for the **"Concerned Folder"** metadata embedded in the ticket description (`Concerned Folder - <folder_name>` or `Concerned Folder: <folder_name>`)
- Collects source files from the resolved folder as AI context
- Calls the GitHub Copilot API directly (token refresh via `_get_copilot_session_token()`)
- Parses JSON code changes returned by the LLM (with control-character sanitisation via `_sanitise_json_control_chars()`)
- Creates feature branch `featureAutoGen/<TICKET-KEY>` from `BASE_BRANCH`
- Commits and pushes AI-generated files
- Creates a PR on Bitbucket via REST API
- Posts a comment on the Jira ticket
- Transitions the ticket to **"Unit Testing"**

**Merge hard stop:** `_assert_no_merge()` raises `RuntimeError` unconditionally. It is referenced before every git/PR operation as a safety net.

**Usage:**
```bash
python watcher.py BLITZ-12442
python watcher.py BLITZ-12442 --interval 30
python watcher.py BLITZ-12442 --status "Ready for Dev"
```

**State file format (`watcher_state.json`):**
```json
{
  "processed": {
    "BLITZ-12442": {
      "pr_url": "https://bitbucket.org/.../pull-requests/42",
      "processed_at": "2026-04-24T10:00:00"
    }
  }
}
```

---

### `main.py` — FastAPI Webhook Server

Exposes a REST API for receiving live Jira webhook events.

```
GET  /health          → {"status": "ok"}
POST /webhook/jira    → trigger pipeline for matching events
```

The server delegates processing to `app/workers/job_processor.py` via FastAPI `BackgroundTasks` — the HTTP response returns immediately (non-blocking).

**Usage:**
```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

---

### `app/jira/`

#### `client.py` — `JiraClient`

Thin wrapper around Jira REST API v3.

| Method | Description |
|--------|-------------|
| `get_issue(issue_key)` | Fetches and normalises a Jira issue. Handles Atlassian Document Format (ADF) → plain text conversion for `description` and `acceptance_criteria`. Tries multiple custom field IDs for acceptance criteria (`customfield_10016`, `10014`, `10500`). |
| `add_comment(issue_key, body)` | Posts a plain-text comment using the ADF `doc` structure. |
| `transition_issue(issue_key, status_name)` | Lists available transitions and moves the ticket to the named status. |

Normalised issue dict:
```python
{
    "key": "BLITZ-12442",
    "summary": "...",
    "description": "...",          # plain text, ADF stripped
    "acceptance_criteria": "...",  # plain text
    "status": "In Dev",
    "labels": [...],
    "issue_type": "Story",
    "priority": "High",
    "reporter": "Jane Doe",
    "assignee": "John Smith",
    "url": "https://your-org.atlassian.net/browse/BLITZ-12442",
}
```

#### `webhook_handler.py` — FastAPI Router

Mounted at `/webhook/jira`.

**Trigger conditions:**
- `status == "Ready for Dev"` **OR** `"auto-dev"` label present

**Optional HMAC verification:** When `JIRA_WEBHOOK_SECRET` is set, every request is verified against `X-Hub-Signature` using HMAC-SHA256.

---

### `app/ai/`

#### `code_generator.py` — `AIAgent`

Interfaces with the GitHub Copilot API using an OpenAI-compatible client.

**Session token management:** `_get_session_token()` exchanges the long-lived OAuth token for a 30-minute session token. It caches and auto-refreshes 5 minutes before expiry.

| Method | Description |
|--------|-------------|
| `generate_pr_description(issue)` | Returns a structured markdown PR description (What / Why / How to test / Jira link). Max 600 tokens. |
| `generate_code_changes(issue, readme, relevant_files)` | Returns `[{"path": "...", "content": "..."}]` — full file replacements. Max 4096 tokens. |
| `generate_tests(issue, changes)` | Generates unit test files for changed source files. Same return structure. |

#### `context_builder.py` — `ContextBuilder`

Identifies the most relevant source files for the LLM context.

**Algorithm:**
1. Extracts tokens from `summary`, `description`, `acceptance_criteria` (stop-word filtered, alphanumeric only).
2. Walks the repository, skipping `_SKIP_DIRS` (`.git`, `node_modules`, `__pycache__`, etc.).
3. Scores each file by keyword frequency in its path and content.
4. Returns the top-N files (configurable) with content truncated to `max_file_chars`.

Supported extensions: `.py .js .ts .jsx .tsx .java .go .rb .php .cs .cpp .c .h .rs .yaml .yml .json .toml .ini .cfg .sh .bash .md`

| Method | Description |
|--------|-------------|
| `load_readme()` | Returns truncated content of `README.md` from repo root. |
| `find_relevant_files(issue)` | Returns `[{"path": ..., "content": ...}]` — keyword-scored top files. |

#### `prompt_builder.py` — `PromptBuilder`

Centralises all LLM prompt templates.

| Template | Purpose |
|----------|---------|
| `_PR_DESCRIPTION_SYSTEM/USER` | Structured PR description generation |
| `_CODE_GEN_SYSTEM/USER` | Code implementation — LLM returns JSON only |
| `_TEST_GEN_SYSTEM/USER` | Unit test generation for changed files |

---

### `app/git/`

#### `branch_manager.py` — `BranchManager`

Creates feature branches using **GitPython** (local) and **PyGithub** (remote).

| Method | Description |
|--------|-------------|
| `create_branch(issue_key, title)` | Creates `feature/<ISSUE_KEY>-<slug>` from `origin/<base_branch>`, pushes, returns branch name. |
| `get_current_branch()` | Returns current HEAD branch name. |

Branch naming: `feature/BLITZ-12442-short-title-slug` (lowercased, non-alphanumeric → `-`, max 50 chars).

#### `code_applier.py` — `CodeApplier`

Writes AI-generated changes, commits, and pushes with GitPython.

| Method | Description |
|--------|-------------|
| `apply_changes(changes, commit_message)` | Writes `[{"path", "content"}]` to disk, stages, commits, pushes. Returns `True` on success. |
| `commit_exists(message_prefix)` | Checks if HEAD commit message starts with a given prefix (idempotency guard). |

#### `pr_creator.py` — `PRCreator`

Opens Pull Requests on **GitHub** via PyGithub.

**Merge hard stop:** At import time, `PullRequest.merge` is monkey-patched to `_MERGE_FORBIDDEN`, which raises `RuntimeError` unconditionally.

| Method | Description |
|--------|-------------|
| `create_pr(branch_name, title, body, draft=True)` | Creates a draft PR. Returns PR object. |
| `update_pr_body(pr, body)` | Replaces the PR description with AI-generated markdown. |

---

### `app/workers/`

#### `job_processor.py` — `process_issue()`

Orchestrates the full 4-phase pipeline.

```
Phase 1  →  Create branch + empty draft PR  →  Post Jira comment (branch + PR link)
Phase 2  →  Generate AI PR description      →  Update PR body
Phase 3  →  Build context → Generate code   →  Commit + push
Phase 4  →  Generate unit tests             →  Commit + push
```

Each phase is independently wrapped in `try/except`. A phase failure is logged but does not stop later phases. If the entire pipeline fails, `_post_failure_comment()` posts a diagnostic comment to the Jira ticket.

---

### `app/config/`

#### `settings.py` — `Settings`

Pydantic `BaseSettings` class with `@lru_cache` singleton. Reads all values from `.env`.

| Setting | Default | Description |
|---------|---------|-------------|
| `jira_url` | — | Jira base URL |
| `jira_user` | — | Jira account email |
| `jira_token` | — | Atlassian API token |
| `jira_webhook_secret` | `""` | Optional HMAC secret |
| `github_token` | — | GitHub PAT with `repo` scope |
| `github_repo` | — | `"org/repo-name"` |
| `repo_local_path` | — | Absolute path to local repo clone |
| `copilot_oauth_token` | `""` | GitHub Copilot OAuth token |
| `copilot_model` | `"claude-sonnet-4.6"` | LLM model identifier |
| `copilot_base_url` | `"https://api.githubcopilot.com"` | Copilot API base URL |
| `base_branch` | `"main"` | Base branch for GitHub PRs |
| `max_context_files` | `10` | Max files sent to LLM |
| `max_file_chars` | `8000` | Max characters per context file |
| `max_readme_chars` | `4000` | Max README characters in LLM context |

---

### `get_copilot_token.py`

One-time Device Flow authentication to obtain a `COPILOT_OAUTH_TOKEN`.

```bash
python get_copilot_token.py
```

1. Requests a device code from GitHub (`Iv1.b507a08c87ecfe98` client ID — same as VS Code/Neovim Copilot).
2. Prints a verification URL and a one-time user code.
3. Polls until the user completes browser authentication.
4. Prints the OAuth token for you to paste into `.env`.

---

### `dummy_trigger.py`

Manual trigger for testing the branch + PR flow without waiting for a Jira status change.

```bash
python dummy_trigger.py https://your-org.atlassian.net/browse/BLITZ-12432
python dummy_trigger.py BLITZ-12432
```

Actions performed:
1. Parses the ticket key from the URL or argument.
2. Fetches issue details from Jira.
3. Creates branch `featureAutoGen/<TICKET-KEY>` from `BB_BASE_BRANCH`.
4. Writes a `.ai-agent-trigger` metadata file and commits it.
5. Pushes to Bitbucket.
6. Opens a PR on Bitbucket with ticket metadata in the description.

> This script does **not** call the AI code generation step. It tests git/PR plumbing only.

---

## Full Pipeline Walkthrough

When `watcher.py` detects status **"In Dev"** for `BLITZ-12442`:

```
[1]  Fetch issue
     └─ GET /rest/api/3/issue/BLITZ-12442
        Returns: summary, description, type, priority, labels, URL

[2]  Parse "Concerned Folder" from description
     └─ Regex: "Concerned Folder - <folder_name>"
        Scopes AI context to the relevant service directory

[3]  Collect context files
     └─ Walks LOCAL_REPO/<concerned_folder>/
        Returns up to MAX_CONTEXT_FILES source files (keyword-scored)

[4]  Create feature branch
     └─ git fetch origin
        git checkout -b featureAutoGen/BLITZ-12442 origin/release/dev
        git push <bitbucket_url> featureAutoGen/BLITZ-12442

[5]  Call GitHub Copilot API
     └─ Exchange COPILOT_OAUTH_TOKEN → short-lived session token
        POST https://api.githubcopilot.com/chat/completions
        Model: claude-sonnet-4.6
        Response: JSON array of {"path", "content"}

[6]  Apply code changes
     └─ Write files to LOCAL_REPO
        git add <files>
        git commit -m "BLITZ-12442: <summary>"
        git push <bitbucket_url> featureAutoGen/BLITZ-12442

[7]  Open Pull Request on Bitbucket
     └─ POST https://api.bitbucket.org/2.0/repositories/.../pullrequests
        source: featureAutoGen/BLITZ-12442 → release/dev

[8]  Post Jira comment
     └─ "Branch: featureAutoGen/BLITZ-12442 | PR: <url>"

[9]  Transition Jira ticket
     └─ GET /transitions → find "Unit Testing" ID
        POST /transitions { id: "<unit-testing-id>" }

[10] Persist state
     └─ watcher_state.json["processed"]["BLITZ-12442"] = { pr_url, processed_at }
```

---

## How to Run

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Obtain Copilot OAuth token (one-time)

```bash
python get_copilot_token.py
# Follow the prompt, paste token into .env as COPILOT_OAUTH_TOKEN=...
```

### 3. Configure `.env`

```bash
cp .env.example .env
# Fill in all required values
```

### 4a. Run the autonomous watcher

```bash
python watcher.py BLITZ-12442
python watcher.py BLITZ-12442 --interval 30       # poll every 30s
python watcher.py BLITZ-12442 --status "In Dev"   # custom trigger status
```

### 4b. Run the FastAPI webhook server

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
# Configure a Jira webhook pointing to http://<server>:8000/webhook/jira
```

### 5. Test branch + PR flow manually

```bash
python dummy_trigger.py BLITZ-12442
```

---

## Configuration Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `JIRA_URL` | Yes | — | Jira base URL |
| `JIRA_USER` | Yes | — | Jira account email |
| `JIRA_TOKEN` | Yes | — | Atlassian API token |
| `JIRA_WEBHOOK_SECRET` | No | `""` | HMAC secret for webhook verification |
| `BB_USERNAME` | Yes | — | Bitbucket username |
| `BB_API_TOKEN` | Yes | — | Bitbucket app password |
| `BB_WORKSPACE` | Yes | — | Bitbucket workspace slug |
| `BB_REPO_SLUG` | Yes | — | Bitbucket repository slug |
| `BB_LOCAL_REPO` | Yes | — | Absolute path to local Bitbucket repo clone |
| `BB_BASE_BRANCH` | Yes | — | Base branch for Bitbucket PRs (e.g. `release/dev`) |
| `GITHUB_TOKEN` | Yes | — | GitHub PAT with `repo` scope |
| `GITHUB_REPO` | Yes | — | `"org/repo-name"` for GitHub PR creation |
| `REPO_LOCAL_PATH` | Yes | — | Absolute path to local repo clone (used by `app/` modules) |
| `COPILOT_OAUTH_TOKEN` | Yes | — | GitHub Copilot OAuth token |
| `COPILOT_MODEL` | No | `claude-sonnet-4.6` | Model identifier |
| `COPILOT_BASE_URL` | No | `https://api.githubcopilot.com` | Copilot API endpoint |
| `BASE_BRANCH` | No | `main` | Base branch for GitHub PRs |
| `MAX_CONTEXT_FILES` | No | `10` | Max source files sent to LLM |
| `MAX_FILE_CHARS` | No | `8000` | Max characters per context file |
| `MAX_README_CHARS` | No | `4000` | Max README characters in LLM context |

---

## Safety Guarantees

| Guarantee | Implementation |
|-----------|----------------|
| **Never merges PRs** | `PullRequest.merge` is monkey-patched at import in `pr_creator.py`. `_assert_no_merge()` is called before every git/PR operation in `watcher.py`. |
| **Only modifies feature branches** | All changes are committed to `featureAutoGen/<KEY>`. The base branch is never touched. |
| **Idempotent** | `watcher_state.json` ensures each ticket is processed at most once unless manually reset. |
| **Fail-safe phases** | Each pipeline phase is independently `try/except`-wrapped. Phase failures are logged but don't abort later phases. |
| **Webhook signature verification** | HMAC-SHA256 verification on every webhook request when `JIRA_WEBHOOK_SECRET` is set. |
| **No credentials in code** | All secrets loaded exclusively from `.env` via `pydantic-settings`. |
| **Sanitised LLM output** | `_sanitise_json_control_chars()` escapes control characters before JSON parsing to prevent injection. |

---

## Logging

Log format: `YYYY-MM-DD HH:MM:SS  LEVEL     message`

Output goes to both stdout and `watcher.log` simultaneously.

```bash
tail -f watcher.log
```

| Level | Usage |
|-------|-------|
| `INFO` | Normal progress: branch created, PR opened, ticket transitioned |
| `WARNING` | Non-fatal issues: phase failed but pipeline continues |
| `ERROR` / `EXCEPTION` | Fatal pipeline failures |

---

## State Persistence

**Reset all processed tickets:**
```bash
echo '{"processed":{}}' > watcher_state.json
```

**Reset a single ticket:**
```bash
python -c "
import json, pathlib
f = pathlib.Path('watcher_state.json')
s = json.loads(f.read_text())
s['processed'].pop('BLITZ-12442', None)
f.write_text(json.dumps(s, indent=2))
"
```

---

## Requirements

- Python 3.10+
- A local clone of the target repository
- Jira account with API token
- Bitbucket account with API token (Repositories + Pull Requests: Read/Write)
- GitHub PAT with `repo` scope
- GitHub Copilot subscription

---

## Extending the Agent

### Change the trigger status

```bash
# Watcher (CLI)
python watcher.py BLITZ-12442 --status "Ready for Dev"

# Webhook handler (app/jira/webhook_handler.py)
_TRIGGER_STATUS = "Your Status"
```

### Add a pipeline phase

In `app/workers/job_processor.py`:
```python
# ── Phase 5: Your phase ───────────────────────────────────────────────────────
logger.info("[%s] Phase 5 – your description", issue_key)
try:
    # your code
except Exception as exc:
    logger.warning("[%s] Phase 5 failed (continuing): %s", issue_key, exc)
```

### Change the LLM model

```env
COPILOT_MODEL=gpt-4o
```

### Use a different AI provider

Update `COPILOT_BASE_URL` and `COPILOT_OAUTH_TOKEN` in `.env` to point to any OpenAI-compatible endpoint.
