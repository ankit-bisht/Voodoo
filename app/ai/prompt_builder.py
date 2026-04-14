from __future__ import annotations

from typing import Any

from app.config.settings import settings

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_PR_DESCRIPTION_SYSTEM = (
    "You are a senior software engineer writing a GitHub Pull Request description. "
    "Be concise, precise, and structured. Use markdown."
)

_PR_DESCRIPTION_USER = """\
Write a GitHub Pull Request description for the following Jira ticket.

## Jira Ticket
**Key:** {key}
**URL:** {url}
**Summary:** {summary}
**Type:** {issue_type} | **Priority:** {priority}
**Reporter:** {reporter}

### Description
{description}

### Acceptance Criteria
{acceptance_criteria}

---

The PR description should include:
1. **What** – a short (2-3 sentence) summary of the change
2. **Why** – the reason / business context
3. **How to test** – basic steps for a reviewer to verify the behaviour
4. A link back to the Jira ticket

Keep it under 300 words.
"""

_CODE_GEN_SYSTEM = """\
You are a senior software engineer implementing a Jira ticket.
You will be given:
- The Jira ticket (summary, description, acceptance criteria)
- The project README
- Relevant source files

Your job is to implement the required changes.

Rules:
1. Modify ONLY the files provided. Do NOT invent new files unless strictly necessary.
2. Follow the existing coding style (indentation, naming, patterns).
3. Return your answer as a JSON array with the structure:
   [{"path": "relative/path/to/file.py", "content": "full file content here"}, ...]
4. Return ONLY valid JSON. No markdown fences, no explanation outside the JSON.
5. If no code changes are needed, return an empty array: []
"""

_CODE_GEN_USER = """\
## Jira Ticket
**Key:** {key}  |  **URL:** {url}
**Summary:** {summary}
**Type:** {issue_type}

### Description
{description}

### Acceptance Criteria
{acceptance_criteria}

---

## Project README
{readme}

---

## Relevant Source Files
{files_block}

---

Implement the ticket. Return ONLY a JSON array as described in the system prompt.
"""

_TEST_GEN_SYSTEM = """\
You are a senior software engineer writing unit tests.
Given changed source files, generate corresponding unit/integration test functions.
Follow the project's existing test style where possible.
Return your answer as a JSON array:
[{"path": "tests/test_<module>.py", "content": "full test file content"}, ...]
Return ONLY valid JSON. No markdown fences, no explanation.
"""

_TEST_GEN_USER = """\
The following files were changed as part of Jira ticket {key} ("{summary}").
Write unit tests for them.

## Changed Files
{files_block}
"""


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class PromptBuilder:

    def build_pr_description_messages(self, issue: dict[str, Any]) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": _PR_DESCRIPTION_SYSTEM},
            {"role": "user", "content": _PR_DESCRIPTION_USER.format(**_issue_fmt(issue))},
        ]

    def build_code_gen_messages(
        self,
        issue: dict[str, Any],
        readme: str,
        relevant_files: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        files_block = _format_files_block(relevant_files)
        user = _CODE_GEN_USER.format(
            **_issue_fmt(issue),
            readme=readme or "_No README found_",
            files_block=files_block or "_No relevant files found_",
        )
        return [
            {"role": "system", "content": _CODE_GEN_SYSTEM},
            {"role": "user", "content": user},
        ]

    def build_test_gen_messages(
        self,
        issue: dict[str, Any],
        changed_files: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        files_block = _format_files_block(changed_files)
        user = _TEST_GEN_USER.format(
            key=issue.get("key", ""),
            summary=issue.get("summary", ""),
            files_block=files_block,
        )
        return [
            {"role": "system", "content": _TEST_GEN_SYSTEM},
            {"role": "user", "content": user},
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _issue_fmt(issue: dict[str, Any]) -> dict[str, str]:
    return {
        "key": issue.get("key", ""),
        "url": issue.get("url", ""),
        "summary": issue.get("summary", ""),
        "description": issue.get("description", "_No description provided_"),
        "acceptance_criteria": issue.get("acceptance_criteria", "_Not specified_"),
        "issue_type": issue.get("issue_type", ""),
        "priority": issue.get("priority", ""),
        "reporter": issue.get("reporter", ""),
    }


def _format_files_block(files: list[dict[str, str]]) -> str:
    parts: list[str] = []
    for f in files:
        path = f.get("path", "unknown")
        content = f.get("content", "")
        parts.append(f"### {path}\n```\n{content}\n```")
    return "\n\n".join(parts)
