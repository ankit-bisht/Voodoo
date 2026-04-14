"""
dummy_trigger.py
----------------
Accepts a Jira ticket URL or key, fetches issue details, creates a feature
branch named featureAutoGen/<TICKET-KEY> from release/dev, pushes to
Bitbucket, and opens a PR.

Usage:
  python dummy_trigger.py https://sony-liv.atlassian.net/browse/BLITZ-12432
  python dummy_trigger.py BLITZ-12432
"""

import os
import re
import datetime
import subprocess
import sys
import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# ── Configuration (read from .env) ───────────────────────────────────────────
BB_USERNAME   = os.environ["BB_USERNAME"]
BB_API_TOKEN  = os.environ["BB_API_TOKEN"]
BB_WORKSPACE  = os.environ["BB_WORKSPACE"]
BB_REPO_SLUG  = os.environ["BB_REPO_SLUG"]
LOCAL_REPO    = os.environ["BB_LOCAL_REPO"]
BASE_BRANCH   = os.environ["BB_BASE_BRANCH"]

JIRA_URL      = os.environ["JIRA_URL"].rstrip("/")
JIRA_USER     = os.environ["JIRA_USER"]
JIRA_TOKEN    = os.environ["JIRA_TOKEN"]


def parse_ticket_key(arg: str) -> str:
    """Extract ticket key from a URL like .../browse/BLITZ-12432 or plain BLITZ-12432."""
    match = re.search(r"([A-Z]+-\d+)", arg)
    if not match:
        print(f"ERROR: Could not find a Jira ticket key in: {arg}")
        sys.exit(1)
    return match.group(1)


def fetch_jira_issue(ticket_key: str) -> dict:
    """Fetch issue details from Jira REST API v3."""
    print(f"  Fetching Jira issue {ticket_key}...")
    url = f"{JIRA_URL}/rest/api/3/issue/{ticket_key}"
    resp = requests.get(url, auth=(JIRA_USER, JIRA_TOKEN), headers={"Accept": "application/json"})
    if resp.status_code != 200:
        print(f"  ERROR fetching Jira issue: {resp.status_code} {resp.text[:300]}")
        sys.exit(1)
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
        "key":        ticket_key,
        "summary":    fields.get("summary", ""),
        "status":     fields.get("status", {}).get("name", ""),
        "issue_type": (fields.get("issuetype") or {}).get("name", ""),
        "priority":   (fields.get("priority") or {}).get("name", ""),
        "description": description,
        "url":        f"{JIRA_URL}/browse/{ticket_key}",
    }

BB_AUTH           = (BB_USERNAME, BB_API_TOKEN)
HEADERS_BEARER    = {"Authorization": f"Bearer {BB_API_TOKEN}", "Content-Type": "application/json"}
HEADERS_BASIC     = {"Content-Type": "application/json"}


def run(cmd: list[str], cwd: str = LOCAL_REPO) -> str:
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  STDERR: {result.stderr.strip()}")
        raise RuntimeError(f"Command failed: {' '.join(cmd)}")
    if result.stdout.strip():
        print(f"  {result.stdout.strip()}")
    return result.stdout.strip()


def step1_create_and_push_branch(branch_name: str, issue: dict) -> None:
    print(f"\n[Phase 1] Creating branch '{branch_name}' from '{BASE_BRANCH}'...")

    run(["git", "fetch", "origin"])

    # Delete local branch if it already exists
    existing = subprocess.run(
        ["git", "branch", "--list", branch_name],
        cwd=LOCAL_REPO, capture_output=True, text=True
    )
    if existing.stdout.strip():
        print(f"  Branch '{branch_name}' already exists locally — switching away and deleting it.")
        subprocess.run(["git", "stash"], cwd=LOCAL_REPO, capture_output=True, text=True)
        run(["git", "checkout", BASE_BRANCH])
        run(["git", "branch", "-D", branch_name])

    run(["git", "checkout", "-b", branch_name, f"origin/{BASE_BRANCH}"])

    remote_url = f"https://{BB_USERNAME}:{BB_API_TOKEN}@bitbucket.org/{BB_WORKSPACE}/{BB_REPO_SLUG}.git"
    run(["git", "push", "--force", remote_url, f"{branch_name}:{branch_name}"])

    # Add a trigger file so branch differs from base (required for PR creation)
    dummy_file = os.path.join(LOCAL_REPO, ".ai-agent-trigger")
    with open(dummy_file, "w") as f:
        f.write(
            f"Ticket  : {issue['key']}\n"
            f"Summary : {issue['summary']}\n"
            f"Status  : {issue['status']}\n"
            f"Triggered: {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n"
        )
    run(["git", "add", ".ai-agent-trigger"])
    run(["git", "commit", "-m", f"{issue['key']}: AI agent trigger commit"])
    run(["git", "push", remote_url, f"{branch_name}:{branch_name}"])

    print(f"  ✓ Branch '{branch_name}' pushed to Bitbucket.")


def step2_create_pr(branch_name: str, issue: dict) -> dict:
    print(f"\n[Phase 2] Creating PR on Bitbucket via REST API...")

    url = f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/{BB_REPO_SLUG}/pullrequests"

    description = (
        f"## {issue['key']}: {issue['summary']}\n\n"
        f"**Jira:** [{issue['key']}]({issue['url']})  \n"
        f"**Status:** {issue['status']}  \n"
        f"**Type:** {issue['issue_type']}  |  **Priority:** {issue['priority']}  \n\n"
    )
    if issue.get("description"):
        description += f"### Description\n{issue['description'][:1000]}\n\n"
    description += "> _This PR was auto-created by the Jira AI Agent 'Ready for Dev' trigger._"

    payload = {
        "title": f"{issue['key']}: {issue['summary']}",
        "description": description,
        "source": {
            "branch": {"name": branch_name},
            "repository": {"full_name": f"{BB_WORKSPACE}/{BB_REPO_SLUG}"}
        },
        "destination": {"branch": {"name": BASE_BRANCH}},
        "reviewers": [],
        "close_source_branch": False,
    }

    auth_attempts = [
        ("Basic user:token",   {"auth": BB_AUTH, "headers": HEADERS_BASIC}),
        ("Bearer token",       {"headers": HEADERS_BEARER}),
        ("x-token-auth basic", {"auth": ("x-token-auth", BB_API_TOKEN), "headers": HEADERS_BASIC}),
    ]

    for label, kwargs in auth_attempts:
        resp = requests.post(url, json=payload, **kwargs)
        print(f"  [{label}] → {resp.status_code}")
        if resp.status_code in (200, 201):
            pr = resp.json()
            print(f"  ✓ PR created: #{pr['id']} — {pr['links']['html']['href']}")
            return pr
        elif resp.status_code == 400 and "already has an open pull request" in resp.text:
            print("  PR already exists — fetching existing PR...")
            encoded = requests.utils.quote(branch_name)
            list_url = (
                f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/{BB_REPO_SLUG}"
                f"/pullrequests?q=source.branch.name%3D%22{encoded}%22%20AND%20state%3D%22OPEN%22"
            )
            list_resp = requests.get(list_url, **kwargs)
            pr = list_resp.json()["values"][0]
            print(f"  ✓ Existing PR: #{pr['id']} — {pr['links']['html']['href']}")
            return pr
        else:
            print(f"       Response: {resp.text[:300]}")

    print("  ✗ All auth methods failed.")
    sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python dummy_trigger.py <jira-url-or-ticket-key>")
        print("  e.g. python dummy_trigger.py https://sony-liv.atlassian.net/browse/BLITZ-12432")
        print("  e.g. python dummy_trigger.py BLITZ-12432")
        sys.exit(1)

    ticket_key  = parse_ticket_key(sys.argv[1])
    branch_name = f"featureAutoGen/{ticket_key}"

    print("=" * 60)
    print(f"  Jira AI Agent — 'Ready for Dev' trigger")
    print(f"  Ticket : {ticket_key}")
    print(f"  Branch : {branch_name}  →  {BASE_BRANCH}")
    print(f"  Repo   : {BB_WORKSPACE}/{BB_REPO_SLUG}")
    print("=" * 60)

    issue = fetch_jira_issue(ticket_key)
    print(f"  ✓ Fetched: [{issue['key']}] {issue['summary']} ({issue['status']})")

    step1_create_and_push_branch(branch_name, issue)
    pr = step2_create_pr(branch_name, issue)

    print("\n" + "=" * 60)
    print("  Pipeline complete!")
    print(f"  Ticket : {ticket_key} — {issue['summary']}")
    print(f"  Branch : {branch_name}")
    print(f"  PR     : {pr['links']['html']['href']}")
    print("=" * 60)
