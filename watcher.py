"""
watcher.py
----------
Autonomous Jira watcher daemon.

- Polls a single Jira ticket every POLL_INTERVAL seconds
- When status changes to "In Dev", auto-creates branch + PR
- Maintains state file to avoid reprocessing
- Zero human intervention required

Usage:
    python watcher.py BLITZ-12442
    python watcher.py BLITZ-12442 --interval 30
    python watcher.py BLITZ-12442 --status "Ready for Dev"
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from openai import OpenAI

# ── Bootstrap ────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / "watcher.log"),
    ],
)
log = logging.getLogger("watcher")

# ── Config ───────────────────────────────────────────────────────────────────
JIRA_URL      = os.environ["JIRA_URL"].rstrip("/")
JIRA_USER     = os.environ["JIRA_USER"]
JIRA_TOKEN    = os.environ["JIRA_TOKEN"]

BB_USERNAME   = os.environ["BB_USERNAME"]
BB_API_TOKEN  = os.environ["BB_API_TOKEN"]
BB_WORKSPACE  = os.environ["BB_WORKSPACE"]
BB_REPO_SLUG  = os.environ["BB_REPO_SLUG"]
LOCAL_REPO    = os.environ["BB_LOCAL_REPO"]
BASE_BRANCH   = os.environ["BB_BASE_BRANCH"]

STATE_FILE    = BASE_DIR / "watcher_state.json"

JIRA_AUTH     = (JIRA_USER, JIRA_TOKEN)
JIRA_HEADERS  = {"Accept": "application/json"}
BB_AUTH       = (BB_USERNAME, BB_API_TOKEN)
BB_HEADERS    = {"Content-Type": "application/json"}

# GitHub Copilot API — same account as your VS Code Copilot subscription
COPILOT_OAUTH_TOKEN = os.environ.get("COPILOT_OAUTH_TOKEN", "")
COPILOT_BASE_URL    = os.environ.get("COPILOT_BASE_URL", "https://api.githubcopilot.com")
COPILOT_MODEL       = os.environ.get("COPILOT_MODEL", "claude-sonnet-4.6")

# Will hold the short-lived Copilot session token (refreshed automatically)
_copilot_session: dict = {}


def _get_copilot_session_token() -> str:
    """Exchange the GitHub OAuth token for a short-lived Copilot session token."""
    global _copilot_session
    now = time.time()
    # Reuse if still valid (tokens last ~30 min; refresh 5 min early)
    if _copilot_session.get("token") and now < _copilot_session.get("expires_at", 0) - 300:
        return _copilot_session["token"]

    resp = requests.get(
        "https://api.github.com/copilot_internal/v2/token",
        headers={
            "Authorization": f"token {COPILOT_OAUTH_TOKEN}",
            "Accept": "application/json",
            "Editor-Version": "vscode/1.99.0",
            "Copilot-Integration-Id": "vscode-chat",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    _copilot_session = {
        "token":      data["token"],
        "expires_at": data.get("expires_at", now + 1800),
    }
    log.info("Copilot session token refreshed (expires at %s)",
             datetime.datetime.fromtimestamp(_copilot_session["expires_at"]).strftime("%H:%M:%S"))
    return _copilot_session["token"]

# Repo subdirectories to collect as context for AI code generation
CODE_CONTEXT_DIRS = [
    "crunchyIngestion/handlers",
    "crunchyIngestion/app",
    "crunchyIngestion/config",
]


# ── State management ─────────────────────────────────────────────────────────

def load_state() -> dict:
    """Load persisted state from disk."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"processed": {}}


def save_state(state: dict) -> None:
    """Persist state to disk atomically."""
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def is_processed(state: dict, ticket_key: str) -> bool:
    return ticket_key in state["processed"]


def mark_processed(state: dict, ticket_key: str, pr_url: str) -> None:
    state["processed"][ticket_key] = {
        "pr_url": pr_url,
        "processed_at": datetime.datetime.now().isoformat(),
    }
    save_state(state)


# ── Jira helpers ─────────────────────────────────────────────────────────────

def get_ticket_status(ticket_key: str) -> str:
    """Return the current status name of a Jira ticket."""
    url = f"{JIRA_URL}/rest/api/3/issue/{ticket_key}?fields=status"
    resp = requests.get(url, auth=JIRA_AUTH, headers=JIRA_HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()["fields"]["status"]["name"]


def fetch_issue(ticket_key: str) -> dict:
    """Fetch and normalise a single Jira issue."""
    url = f"{JIRA_URL}/rest/api/3/issue/{ticket_key}"
    resp = requests.get(url, auth=JIRA_AUTH, headers=JIRA_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    fields = data.get("fields", {})

    def adf_to_text(node) -> str:
        if isinstance(node, str):
            return node
        if isinstance(node, dict):
            if node.get("type") == "text":
                return node.get("text", "")
            return " ".join(adf_to_text(c) for c in node.get("content", []))
        if isinstance(node, list):
            return " ".join(adf_to_text(c) for c in node)
        return ""

    description = fields.get("description") or ""
    if isinstance(description, dict):
        description = adf_to_text(description)

    return {
        "key":         ticket_key,
        "summary":     fields.get("summary", ""),
        "status":      fields.get("status", {}).get("name", ""),
        "issue_type":  (fields.get("issuetype") or {}).get("name", ""),
        "priority":    (fields.get("priority") or {}).get("name", ""),
        "description": description,
        "url":         f"{JIRA_URL}/browse/{ticket_key}",
    }


def transition_jira_issue(ticket_key: str, transition_name: str) -> None:
    """Move a Jira ticket to the given status by name."""
    # First fetch available transitions to find the right ID
    url = f"{JIRA_URL}/rest/api/3/issue/{ticket_key}/transitions"
    resp = requests.get(url, auth=JIRA_AUTH, headers=JIRA_HEADERS, timeout=10)
    resp.raise_for_status()
    transitions = resp.json().get("transitions", [])
    match = next((t for t in transitions if t["name"].lower() == transition_name.lower()), None)
    if not match:
        available = [t["name"] for t in transitions]
        raise RuntimeError(f"Transition '{transition_name}' not found. Available: {available}")

    transition_url = f"{JIRA_URL}/rest/api/3/issue/{ticket_key}/transitions"
    body = {"transition": {"id": match["id"]}}
    r = requests.post(
        transition_url,
        auth=JIRA_AUTH,
        headers={**JIRA_HEADERS, "Content-Type": "application/json"},
        json=body,
        timeout=10,
    )
    if r.status_code == 204:
        log.info("[%s] Ticket moved to '%s'", ticket_key, transition_name)
    else:
        raise RuntimeError(f"Transition failed: {r.status_code} {r.text[:200]}")


def post_jira_comment(ticket_key: str, message: str) -> None:
    """Post a comment to a Jira ticket."""
    url = f"{JIRA_URL}/rest/api/3/issue/{ticket_key}/comment"
    body = {"body": {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": message}]}
    ]}}
    try:
        requests.post(url, auth=JIRA_AUTH, headers={**JIRA_HEADERS, "Content-Type": "application/json"}, json=body, timeout=10)
    except Exception as e:
        log.warning("Could not post Jira comment on %s: %s", ticket_key, e)


# ── MERGE HARD STOP ─────────────────────────────────────────────────────────
# This agent NEVER merges any PR. Period.
# This guard is called before every PR / git operation as a safety net.

def _assert_no_merge(*args, **kwargs) -> None:  # noqa: ARG001
    """Hard stop — raises unconditionally if called. No merges, ever."""
    raise RuntimeError(
        "HARD STOP: Merging a PR is strictly forbidden. "
        "This agent only creates branches and opens PRs. "
        "A human must review and merge manually."
    )


# ── Git helpers ───────────────────────────────────────────────────────────────

def run_git(cmd: list[str]) -> str:
    """Run a git command in LOCAL_REPO; raises on failure."""
    result = subprocess.run(cmd, cwd=LOCAL_REPO, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(cmd[1:])} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def create_and_push_branch(branch_name: str, issue: dict) -> None:
    """Create branch from BASE_BRANCH, add trigger commit, push."""
    log.info("[%s] Fetching origin...", issue["key"])
    run_git(["git", "fetch", "origin"])

    # If branch already exists locally, switch away and delete
    existing = subprocess.run(
        ["git", "branch", "--list", branch_name],
        cwd=LOCAL_REPO, capture_output=True, text=True
    )
    if existing.stdout.strip():
        subprocess.run(["git", "stash"], cwd=LOCAL_REPO, capture_output=True, text=True)
        run_git(["git", "checkout", BASE_BRANCH])
        run_git(["git", "branch", "-D", branch_name])

    run_git(["git", "checkout", "-b", branch_name, f"origin/{BASE_BRANCH}"])
    log.info("[%s] Branch %s created", issue["key"], branch_name)

    # Push empty branch first
    remote_url = f"https://{BB_USERNAME}:{BB_API_TOKEN}@bitbucket.org/{BB_WORKSPACE}/{BB_REPO_SLUG}.git"
    run_git(["git", "push", "--force", remote_url, f"{branch_name}:{branch_name}"])

    # Write trigger + ticket metadata file
    trigger_file = Path(LOCAL_REPO) / ".ai-agent-trigger"
    trigger_file.write_text(
        f"Ticket   : {issue['key']}\n"
        f"Summary  : {issue['summary']}\n"
        f"Status   : {issue['status']}\n"
        f"Type     : {issue['issue_type']}\n"
        f"Priority : {issue['priority']}\n"
        f"URL      : {issue['url']}\n"
        f"Triggered: {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n"
    )
    run_git(["git", "add", ".ai-agent-trigger"])
    run_git(["git", "commit", "-m", f"{issue['key']}: AI agent auto-trigger commit"])
    run_git(["git", "push", remote_url, f"{branch_name}:{branch_name}"])
    log.info("[%s] Branch %s pushed to Bitbucket", issue["key"], branch_name)


# ── Bitbucket PR ──────────────────────────────────────────────────────────────

def create_pr(branch_name: str, issue: dict) -> str:
    """Create a PR on Bitbucket; returns the PR URL."""
    url = f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/{BB_REPO_SLUG}/pullrequests"

    description = (
        f"## {issue['key']}: {issue['summary']}\n\n"
        f"**Jira:** [{issue['key']}]({issue['url']})  \n"
        f"**Status:** {issue['status']}  \n"
        f"**Type:** {issue['issue_type']}  |  **Priority:** {issue['priority']}  \n\n"
    )
    if issue.get("description"):
        description += f"### Description\n{issue['description'][:1500]}\n\n"
    description += "> _Auto-created by Jira AI Agent watcher on status → **In Dev**._"

    payload = {
        "title": f"{issue['key']}: {issue['summary']}",
        "description": description,
        "source": {
            "branch": {"name": branch_name},
            "repository": {"full_name": f"{BB_WORKSPACE}/{BB_REPO_SLUG}"},
        },
        "destination": {"branch": {"name": BASE_BRANCH}},
        "reviewers": [],
        "close_source_branch": False,
    }

    resp = requests.post(url, json=payload, auth=BB_AUTH, headers=BB_HEADERS)

    if resp.status_code in (200, 201):
        pr = resp.json()
        pr_url = pr["links"]["html"]["href"]
        # Sanity-check: agent must never auto-merge
        assert "merge" not in pr_url.lower(), "HARD STOP: unexpected merge URL in PR response"
        log.info("[%s] PR created: #%s — %s", issue["key"], pr["id"], pr_url)
        return pr_url

    if resp.status_code == 400 and "already has an open pull request" in resp.text:
        # PR already exists — find it
        encoded = requests.utils.quote(branch_name)
        list_url = (
            f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/{BB_REPO_SLUG}"
            f"/pullrequests?q=source.branch.name%3D%22{encoded}%22%20AND%20state%3D%22OPEN%22"
        )
        list_resp = requests.get(list_url, auth=BB_AUTH, headers=BB_HEADERS)
        pr = list_resp.json()["values"][0]
        pr_url = pr["links"]["html"]["href"]
        log.info("[%s] Existing PR: #%s — %s", issue["key"], pr["id"], pr_url)
        return pr_url

    raise RuntimeError(f"Bitbucket PR creation failed {resp.status_code}: {resp.text[:300]}")


# ── AI code generation ────────────────────────────────────────────────────────

def _sanitise_json_control_chars(s: str) -> str:
    """
    Walk the raw LLM response character-by-character.
    Inside JSON string values, escape any literal control characters
    (the LLM sometimes emits real newlines / tabs / other ctrl chars inside strings).
    """
    out = []
    in_string = False
    escape_next = False
    _ESC = {'\n': '\\n', '\r': '\\r', '\t': '\\t'}
    for ch in s:
        if escape_next:
            out.append(ch)
            escape_next = False
        elif ch == '\\' and in_string:
            out.append(ch)
            escape_next = True
        elif ch == '"':
            in_string = not in_string
            out.append(ch)
        elif in_string and ord(ch) < 0x20:
            # Control character inside a JSON string — must be escaped
            out.append(_ESC.get(ch, f'\\u{ord(ch):04x}'))
        else:
            out.append(ch)
    return ''.join(out)


def collect_context_files() -> list[dict]:
    """Read .py files from CODE_CONTEXT_DIRS; return [{path, content}]."""
    files = []
    repo = Path(LOCAL_REPO)
    for dir_rel in CODE_CONTEXT_DIRS:
        dir_path = repo / dir_rel
        if not dir_path.exists():
            continue
        for py_file in sorted(dir_path.rglob("*.py")):
            if "__pycache__" in py_file.parts:
                continue
            try:
                content = py_file.read_text(errors="replace")
                files.append({"path": str(py_file.relative_to(repo)), "content": content[:6000]})
            except Exception:
                pass
    return files


def generate_code_changes_ai(issue: dict) -> list[dict]:
    """Call GitHub Copilot to implement the ticket. Returns [{path, content}] or []."""
    if not COPILOT_OAUTH_TOKEN:
        log.warning("[%s] COPILOT_OAUTH_TOKEN not set — run get_copilot_token.py to authenticate", issue["key"])
        return []

    session_token = _get_copilot_session_token()
    client = OpenAI(
        api_key=session_token,
        base_url=COPILOT_BASE_URL,
        default_headers={
            "Editor-Version": "vscode/1.99.0",
            "Editor-Plugin-Version": "copilot-chat/0.26.0",
            "Copilot-Integration-Id": "vscode-chat",
            "OpenAI-Intent": "conversation-panel",
        },
    )
    context_files = collect_context_files()
    log.info("[%s] Sending %d file(s) to GitHub Copilot (%s) for code generation", issue["key"], len(context_files), COPILOT_MODEL)

    files_block = "\n\n".join(
        f"### {f['path']}\n```python\n{f['content']}\n```" for f in context_files
    )

    system = (
        "You are a code-generation engine. You output ONLY raw JSON — nothing else.\n"
        "OUTPUT FORMAT: A JSON array of file changes.\n"
        "  [{\"path\": \"relative/path.py\", \"content\": \"<full file content>\"}]\n"
        "STRICT RULES:\n"
        "- Your ENTIRE response must be a single valid JSON array starting with [ and ending with ].\n"
        "- Do NOT write any explanation, analysis, markdown, headings, or prose.\n"
        "- Do NOT wrap the JSON in code fences (no ``` markers).\n"
        "- Modify ONLY the files shown. Follow existing code style exactly.\n"
        "- If no changes are needed, output exactly: []"
    )

    user = (
        f"## Jira Ticket: {issue['key']}\n"
        f"**Summary:** {issue['summary']}\n"
        f"**Type:** {issue['issue_type']} | **Priority:** {issue['priority']}\n\n"
        f"### Description\n{issue.get('description', '_No description_')}\n\n"
        f"---\n\n"
        f"## Source Files\n{files_block}\n\n"
        f"Implement the ticket changes. Respond with the JSON array ONLY. Start your response with `[`."
    )

    resp = client.chat.completions.create(
        model=COPILOT_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=8192,
        temperature=0.1,
    )
    raw = (resp.choices[0].message.content or "").strip()
    finish_reason = resp.choices[0].finish_reason
    if finish_reason == "length":
        log.warning("[%s] Copilot response was truncated (finish_reason=length) — attempting truncation recovery", issue["key"])
        # Try to salvage complete JSON objects from a truncated array
        # Find the last complete object (ends with "}")
        last_brace = raw.rfind("}")
        if last_brace != -1:
            raw = raw[: last_brace + 1] + "]"

    # Strip markdown fences if the model added them
    if raw.endswith("```"):
        raw = raw[: raw.rfind("```")].rstrip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
    raw = raw.strip()

    def _try_parse(text: str) -> list | None:
        """Try json.loads; return list or None."""
        try:
            result = json.loads(text)
            return result if isinstance(result, list) else None
        except Exception:
            return None

    def _extract_json_array(text: str) -> str:
        """
        Extract the outermost [...] JSON array from text that may contain
        prose before/after the array (common LLM behaviour).
        """
        start = text.find("[")
        if start == -1:
            return text
        # Walk forward to find the matching closing bracket
        depth = 0
        in_str = False
        esc = False
        for i, ch in enumerate(text[start:], start):
            if esc:
                esc = False
                continue
            if ch == "\\" and in_str:
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if not in_str:
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        return text[start : i + 1]
        return text[start:]

    # Strategy 1: direct parse (model returned valid JSON)
    changes = _try_parse(raw)

    # Strategy 2: extract the JSON array (model added prose before/after)
    if changes is None:
        changes = _try_parse(_extract_json_array(raw))

    # Strategy 3: sanitise control chars, then extract array
    if changes is None:
        sanitised = _sanitise_json_control_chars(raw)
        changes = _try_parse(sanitised) or _try_parse(_extract_json_array(sanitised))

    if changes is None:
        log.error(
            "[%s] Could not parse AI response — Raw (500 chars): %.500s",
            issue["key"], raw,
        )
        return []

    valid = [c for c in changes if isinstance(c, dict) and c.get("path")]
    log.info("[%s] AI generated %d file change(s)", issue["key"], len(valid))
    return valid


def apply_and_commit_changes(changes: list[dict], issue: dict, remote_url: str, branch_name: str) -> bool:
    """Write AI-generated files, commit and push. Returns True if committed."""
    if not changes:
        log.info("[%s] No AI code changes to apply", issue["key"])
        return False

    repo = Path(LOCAL_REPO)
    applied = []
    for change in changes:
        rel = change["path"].lstrip("/")
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(change["content"])
        applied.append(rel)
        log.info("[%s] Wrote: %s", issue["key"], rel)

    run_git(["git", "add"] + applied)
    run_git(["git", "commit", "-m",
             f"{issue['key']}: AI-generated code changes\n\n"
             f"Summary: {issue['summary']}\n"
             f"Files: {', '.join(applied)}"])
    run_git(["git", "push", remote_url, f"{branch_name}:{branch_name}"])
    log.info("[%s] Code changes committed and pushed (%d file(s))", issue["key"], len(applied))
    return True


# ── Pipeline ──────────────────────────────────────────────────────────────────

def run_pipeline(ticket_key: str) -> str:
    """Full pipeline for one ticket. Returns PR URL."""
    log.info("[%s] ── Pipeline start ──────────────────────────────────", ticket_key)

    issue = fetch_issue(ticket_key)
    log.info("[%s] Fetched: %s (%s)", ticket_key, issue["summary"], issue["status"])

    branch_name = f"featureAutoGen/{ticket_key}"
    remote_url = f"https://{BB_USERNAME}:{BB_API_TOKEN}@bitbucket.org/{BB_WORKSPACE}/{BB_REPO_SLUG}.git"

    # Phase 1 — create branch with trigger commit
    create_and_push_branch(branch_name, issue)

    # Phase 2 — AI code generation + commit
    log.info("[%s] Phase 2 — AI code generation", ticket_key)
    try:
        ai_changes = generate_code_changes_ai(issue)
        apply_and_commit_changes(ai_changes, issue, remote_url, branch_name)
    except Exception as exc:
        log.warning("[%s] AI code generation failed (continuing without code changes): %s", ticket_key, exc)

    # Phase 3 — open PR
    pr_url = create_pr(branch_name, issue)

    post_jira_comment(
        ticket_key,
        f"🤖 AI Agent auto-triggered on status change to '{issue['status']}'.\n"
        f"Branch: {branch_name}\n"
        f"PR: {pr_url}",
    )

    # ── Auto-transition ticket to Unit Testing ────────────────────────────────
    log.info("[%s] Transitioning ticket to 'Unit Testing'...", ticket_key)
    try:
        transition_jira_issue(ticket_key, "Unit Testing")
        post_jira_comment(
            ticket_key,
            f"✅ AI Agent pipeline complete. Ticket auto-moved to 'Unit Testing'.\n"
            f"Branch: {branch_name}\n"
            f"PR: {pr_url}",
        )
    except Exception as exc:
        log.warning("[%s] Could not transition to Unit Testing: %s", ticket_key, exc)

    log.info("[%s] ── Pipeline complete — PR: %s ─────────────────────", ticket_key, pr_url)
    return pr_url


# ── Watcher loop ──────────────────────────────────────────────────────────────

def watch(ticket_key: str, trigger_status: str, poll_interval: int) -> None:
    state = load_state()

    log.info("═" * 60)
    log.info("  Jira AI Agent Watcher")
    log.info("  Ticket   : %s/%s", JIRA_URL, ticket_key)
    log.info("  Trigger  : status == '%s'", trigger_status)
    log.info("  Interval : %ds", poll_interval)
    log.info("  Repo     : %s/%s  →  %s", BB_WORKSPACE, BB_REPO_SLUG, BASE_BRANCH)
    log.info("  State    : %s", STATE_FILE)
    log.info("═" * 60)

    if is_processed(state, ticket_key):
        info = state["processed"][ticket_key]
        log.info("Ticket %s already processed at %s — PR: %s",
                 ticket_key, info["processed_at"], info["pr_url"])
        log.info("Delete %s to reprocess.", STATE_FILE)
        return

    while True:
        try:
            current_status = get_ticket_status(ticket_key)
            log.info("[%s] Current status: '%s'", ticket_key, current_status)

            if current_status == trigger_status:
                log.info("[%s] Status matched '%s' — running pipeline!", ticket_key, trigger_status)
                try:
                    pr_url = run_pipeline(ticket_key)
                    mark_processed(state, ticket_key, pr_url)
                    log.info("[%s] Done. PR: %s", ticket_key, pr_url)
                    log.info("Watcher exiting — ticket processed successfully.")
                    break
                except Exception as exc:
                    log.exception("[%s] Pipeline failed: %s", ticket_key, exc)
                    try:
                        post_jira_comment(
                            ticket_key,
                            f"❌ AI Agent pipeline failed for {ticket_key}.\nError: {exc}",
                        )
                    except Exception:
                        pass
                    log.info("Retrying pipeline in %ds...", poll_interval)
            else:
                log.info("[%s] Waiting for status '%s'. Sleeping %ds...",
                         ticket_key, trigger_status, poll_interval)

        except KeyboardInterrupt:
            log.info("Watcher stopped by user.")
            break
        except Exception as exc:
            log.error("Poll error: %s — retrying in %ds", exc, poll_interval)

        time.sleep(poll_interval)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jira AI Agent Watcher — polls a single ticket")
    parser.add_argument("ticket", help="Jira ticket key or URL, e.g. BLITZ-12442 or https://...")
    parser.add_argument("--status",   default="In Dev",    help='Status to trigger on (default: "In Dev")')
    parser.add_argument("--interval", default=15, type=int, help="Poll interval in seconds (default: 15)")
    args = parser.parse_args()

    # Accept full URL or plain key
    m = re.search(r"([A-Z]+-\d+)", args.ticket)
    if not m:
        print(f"ERROR: Could not parse ticket key from: {args.ticket}")
        sys.exit(1)
    key = m.group(1)

    watch(
        ticket_key=key,
        trigger_status=args.status,
        poll_interval=args.interval,
    )
