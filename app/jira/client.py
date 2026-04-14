from __future__ import annotations

import logging
from typing import Any

import httpx
from httpx import BasicAuth

from app.config.settings import settings

logger = logging.getLogger(__name__)


class JiraClient:
    """Thin wrapper around the Jira REST API v3."""

    def __init__(self) -> None:
        self._base = settings.jira_url.rstrip("/")
        self._auth = BasicAuth(settings.jira_user, settings.jira_token)
        self._headers = {"Accept": "application/json", "Content-Type": "application/json"}

    # ------------------------------------------------------------------
    # Issue fetching
    # ------------------------------------------------------------------

    def get_issue(self, issue_key: str) -> dict[str, Any]:
        """Return a normalised issue dict with the fields we care about."""
        url = f"{self._base}/rest/api/3/issue/{issue_key}"
        with httpx.Client(auth=self._auth, headers=self._headers, timeout=15) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()

        fields = data.get("fields", {})

        # Acceptance criteria may live in a custom field (customfield_10016 is
        # common, but this is configurable).  We try a few known locations.
        acceptance_criteria = (
            fields.get("customfield_10016")
            or fields.get("customfield_10014")
            or fields.get("customfield_10500")
            or ""
        )
        if isinstance(acceptance_criteria, dict):
            # Sometimes it's a rich-text (ADF) dict – extract plain text
            acceptance_criteria = _adf_to_text(acceptance_criteria)

        description = fields.get("description") or ""
        if isinstance(description, dict):
            description = _adf_to_text(description)

        return {
            "key": data["key"],
            "summary": fields.get("summary", ""),
            "description": description,
            "acceptance_criteria": acceptance_criteria,
            "status": fields.get("status", {}).get("name", ""),
            "labels": fields.get("labels", []),
            "issue_type": fields.get("issuetype", {}).get("name", ""),
            "priority": fields.get("priority", {}).get("name", ""),
            "reporter": fields.get("reporter", {}).get("displayName", ""),
            "assignee": (fields.get("assignee") or {}).get("displayName", "Unassigned"),
            "url": f"{self._base}/browse/{data['key']}",
        }

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------

    def add_comment(self, issue_key: str, body: str) -> None:
        """Post a plain-text comment on the issue."""
        url = f"{self._base}/rest/api/3/issue/{issue_key}/comment"
        payload = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": body}],
                    }
                ],
            }
        }
        with httpx.Client(auth=self._auth, headers=self._headers, timeout=15) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
        logger.info("Posted comment on %s", issue_key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _adf_to_text(node: Any, depth: int = 0) -> str:
    """Recursively extract plain text from an Atlassian Document Format node."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    parts: list[str] = []
    for child in node.get("content", []):
        parts.append(_adf_to_text(child, depth + 1))
    return "\n".join(p for p in parts if p)
