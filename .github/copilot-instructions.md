# GitHub Copilot Instructions — Jira AI Agent

## REQUIRED: Plan Before Every Code Change

**You must always show a plan to the user and receive explicit approval before making any code changes.**

### Plan Format

Before writing, editing, or deleting any code, output a plan in the following exact format:

```
## Implementation Plan

**Goal:** <one-sentence description of what this change achieves>

### Files to Modify
| File | Change |
|------|--------|
| `path/to/file.py` | <what will change and why> |

### Files to Create
| File | Purpose |
|------|---------|
| `path/to/new_file.py` | <what it contains> |

### Files to Delete
| File | Reason |
|------|--------|
| `path/to/old_file.py` | <why it is being removed> |

### Steps
1. <first concrete action>
2. <second concrete action>
...

### What Will NOT Change
- <explicitly list files or areas that remain untouched>

### Risks / Notes
- <any side effects, breaking changes, or decisions that need awareness>

---
Proceed with this plan? (yes / no / modify)
```

**Wait for the user to reply "yes" (or equivalent approval) before writing any code.**
If the user says "modify" or asks for changes to the plan, revise and show the updated plan again before proceeding.

---

## Project Overview

This is a Python autonomous agent that:
- Polls Jira tickets and triggers on status transitions (default: "In Dev")
- Generates code changes via the GitHub Copilot API (Claude Sonnet / GPT-4o)
- Creates feature branches, commits AI-generated code, and opens Pull Requests
- Posts comments back to Jira and transitions tickets forward

**Entry points:**
- `watcher.py` — polling daemon (primary)
- `main.py` — FastAPI webhook server
- `dummy_trigger.py` — manual test trigger
- `get_copilot_token.py` — one-time OAuth token acquisition

---

## Architecture Conventions

### Module Responsibilities (do not cross these boundaries)

| Module | Responsibility |
|--------|---------------|
| `app/config/settings.py` | All config — reads `.env` via Pydantic. Never read env vars directly elsewhere in `app/`. |
| `app/jira/client.py` | All Jira REST API calls |
| `app/jira/webhook_handler.py` | FastAPI routing only — no business logic |
| `app/ai/code_generator.py` | Copilot API calls — session token management |
| `app/ai/context_builder.py` | File selection — no API calls |
| `app/ai/prompt_builder.py` | Prompt templates only — no logic |
| `app/git/branch_manager.py` | Branch creation — GitPython + PyGithub |
| `app/git/code_applier.py` | File writes + git commit/push |
| `app/git/pr_creator.py` | PR creation — GitHub API only |
| `app/workers/job_processor.py` | Orchestration — calls other modules, no direct API calls |
| `watcher.py` | Standalone daemon — self-contained, minimal imports from `app/` |

### Settings

- All new configuration **must** be added to `app/config/settings.py` as a typed `pydantic-settings` field with a sensible default.
- The `settings` singleton is cached with `@lru_cache`. Never instantiate `Settings()` directly.

### Error Handling

- In `job_processor.py`, every pipeline phase must be wrapped in `try/except Exception` to prevent one phase from aborting later phases.
- Log failures with `logger.exception(...)` (not `logger.error`) to capture the full traceback.
- Do not add error handling for things that cannot realistically fail (e.g., string formatting).

---

## Hard Rules — Never Violate

1. **Never merge PRs.** `PullRequest.merge` is already monkey-patched in `pr_creator.py`. Do not remove this patch, work around it, or add any code path that could merge a PR.

2. **Never commit credentials.** All secrets live in `.env`. Never hardcode tokens, passwords, or keys.

3. **Never modify the base branch.** All git writes go to the feature branch only. The base branch (`release/dev`, `main`) is read-only.

4. **Session token caching.** The Copilot session token has a 30-minute TTL. Always use the existing `_get_session_token()` / `_get_copilot_session_token()` caching logic. Do not create a new token on every request.

5. **JSON-only LLM output.** When adding new AI prompts that expect structured data, instruct the LLM to return only valid JSON with no markdown fences. Always run the response through the sanitisation helper before `json.loads()`.

6. **State file atomicity.** When updating `watcher_state.json`, always write to a `.tmp` file first, then rename — matching the existing `save_state()` pattern.

---

## Coding Style

- Python 3.10+. Use `from __future__ import annotations` at the top of every new `app/` module.
- Type hints on all function signatures.
- Use `logging.getLogger(__name__)` — never `print()` in `app/` code.
- Use `httpx.Client` (with `BasicAuth`) for HTTP calls inside `app/`. Use `requests` only in `watcher.py` and `dummy_trigger.py` (they are standalone scripts).
- Docstrings: one-line summary for public methods. No verbose inline comments unless the logic is genuinely non-obvious.
- Follow existing indentation (4 spaces), naming (`snake_case` for functions/vars, `PascalCase` for classes), and import ordering (stdlib → third-party → local).

---

## What Good Plans Look Like

### Example: Adding a new Jira field

```
## Implementation Plan

**Goal:** Expose the `sprint` field from the Jira issue in the normalised issue dict.

### Files to Modify
| File | Change |
|------|--------|
| `app/jira/client.py` | Extract `customfield_10020` (sprint) and add `"sprint"` key to returned dict |
| `app/ai/prompt_builder.py` | Include sprint in `_CODE_GEN_USER` and `_PR_DESCRIPTION_USER` templates |

### Files to Create
None.

### Files to Delete
None.

### Steps
1. In `client.py` `get_issue()`, extract sprint name from `fields.get("customfield_10020", [{}])[0].get("name", "")`
2. Add `"sprint": sprint_name` to the returned dict
3. In `prompt_builder.py`, add `**Sprint:** {sprint}` to both user prompt templates
4. Update `.format()` calls to pass `sprint=issue.get("sprint", "")`

### What Will NOT Change
- All other Jira fields and normalisation logic
- `watcher.py` (uses its own inline `fetch_issue()`)

### Risks / Notes
- `customfield_10020` is the common sprint field but may differ per Jira instance. A missing field returns `""` gracefully.

---
Proceed with this plan? (yes / no / modify)
```

---

## Checklist Before Submitting Any Change

- [ ] Plan was shown and approved by the user
- [ ] No new env vars hardcoded in source files
- [ ] No merge-related code paths added
- [ ] All new config added to `settings.py`
- [ ] Any new pipeline phase is `try/except`-wrapped in `job_processor.py`
- [ ] Logging uses `logger` (not `print`)
- [ ] Type hints on all new functions
- [ ] `from __future__ import annotations` present in new `app/` modules
