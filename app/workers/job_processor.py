from __future__ import annotations

"""
job_processor.py
~~~~~~~~~~~~~~~~
Orchestrates the full Jira → Branch → AI → Code → PR pipeline.

Phases:
  1  Create branch + empty draft PR
  2  Generate AI PR description (replaces stub body)
  3  Generate code changes, commit and push
  4  Generate unit tests, commit and push
"""

import logging
from typing import Any

from app.ai.code_generator import AIAgent
from app.ai.context_builder import ContextBuilder
from app.config.settings import settings
from app.git.branch_manager import BranchManager
from app.git.code_applier import CodeApplier
from app.git.pr_creator import PRCreator
from app.jira.client import JiraClient

logger = logging.getLogger(__name__)


def process_issue(issue_key: str) -> None:
    """
    Entry-point called by the FastAPI BackgroundTask.
    Runs through all four phases for the given Jira issue.
    """
    jira = JiraClient()

    try:
        _run_pipeline(issue_key, jira)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Pipeline failed for %s: %s", issue_key, exc)
        _post_failure_comment(jira, issue_key, exc)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _run_pipeline(issue_key: str, jira: JiraClient) -> None:
    # ── Fetch issue ──────────────────────────────────────────────────────────
    logger.info("[%s] Fetching issue details", issue_key)
    issue = jira.get_issue(issue_key)
    summary: str = issue["summary"]

    branch_manager = BranchManager()
    pr_creator = PRCreator()
    context_builder = ContextBuilder()
    ai_agent = AIAgent()
    code_applier = CodeApplier()

    # ── Phase 1: Create branch + empty draft PR ──────────────────────────────
    logger.info("[%s] Phase 1 – creating branch", issue_key)
    branch_name = branch_manager.create_branch(issue_key, summary)

    logger.info("[%s] Phase 1 – creating draft PR", issue_key)
    pr = pr_creator.create_pr(branch_name=branch_name, title=f"{issue_key}: {summary}")

    jira.add_comment(
        issue_key,
        f"🤖 AI Agent started.\nBranch: `{branch_name}`\nPR: {pr.html_url}",
    )

    # ── Phase 2: AI-generated PR description ─────────────────────────────────
    logger.info("[%s] Phase 2 – generating PR description", issue_key)
    try:
        pr_body = ai_agent.generate_pr_description(issue)
        full_body = _build_pr_body(issue, pr_body)
        pr_creator.update_pr_body(pr, full_body)
        logger.info("[%s] Phase 2 complete", issue_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] Phase 2 failed (continuing): %s", issue_key, exc)

    # ── Phase 3: AI code generation ──────────────────────────────────────────
    logger.info("[%s] Phase 3 – building context and generating code", issue_key)
    try:
        readme = context_builder.load_readme()
        relevant_files = context_builder.find_relevant_files(issue)
        code_changes = ai_agent.generate_code_changes(issue, readme, relevant_files)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] Phase 3 code-gen failed (continuing): %s", issue_key, exc)
        code_changes = []

    if code_changes:
        commit_msg = f"{issue_key}: {summary}"
        applied = code_applier.apply_changes(code_changes, commit_msg)
        if applied:
            logger.info("[%s] Phase 3 – code committed and pushed", issue_key)
        else:
            logger.warning("[%s] Phase 3 – apply_changes returned False", issue_key)
    else:
        logger.info("[%s] Phase 3 – no code changes generated", issue_key)

    # ── Phase 4: Test generation ─────────────────────────────────────────────
    logger.info("[%s] Phase 4 – generating tests", issue_key)
    if code_changes:
        try:
            test_changes = ai_agent.generate_tests(issue, code_changes)
            if test_changes:
                test_commit_msg = f"{issue_key}: add tests"
                code_applier.apply_changes(test_changes, test_commit_msg)
                logger.info("[%s] Phase 4 – tests committed and pushed", issue_key)
            else:
                logger.info("[%s] Phase 4 – no test files generated", issue_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] Phase 4 failed (continuing): %s", issue_key, exc)
    else:
        logger.info("[%s] Phase 4 – skipped (no code changes)", issue_key)

    # ── Done ─────────────────────────────────────────────────────────────────
    jira.add_comment(
        issue_key,
        f"✅ AI Agent finished.\nPR is ready for review: {pr.html_url}",
    )
    logger.info("[%s] Pipeline complete", issue_key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_pr_body(issue: dict[str, Any], ai_description: str) -> str:
    return f"""{ai_description}

---

**Jira:** [{issue['key']}]({issue['url']})
**Type:** {issue.get('issue_type', '')}  |  **Priority:** {issue.get('priority', '')}

> _This PR was auto-generated by the Jira AI Agent._
"""


def _post_failure_comment(jira: JiraClient, issue_key: str, exc: Exception) -> None:
    try:
        jira.add_comment(
            issue_key,
            f"❌ AI Agent pipeline failed for {issue_key}.\nError: {exc}\n\nPlease check the server logs.",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not post failure comment on %s", issue_key)
