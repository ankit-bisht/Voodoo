from __future__ import annotations

import json
import logging
import time
from typing import Any

from openai import OpenAI

from app.ai.prompt_builder import PromptBuilder
from app.config.settings import settings

logger = logging.getLogger(__name__)


class AIAgent:
    """Drives code generation via GitHub Copilot API."""

    _session: dict = {}

    def __init__(self) -> None:
        self._oauth_token = settings.copilot_oauth_token
        self._base_url = settings.copilot_base_url
        self._model = settings.copilot_model
        self._prompt_builder = PromptBuilder()

    def _get_session_token(self) -> str:
        """Exchange the OAuth token for a short-lived Copilot session token."""
        now = time.time()
        if AIAgent._session.get("token") and now < AIAgent._session.get("expires_at", 0) - 300:
            return AIAgent._session["token"]
        import requests as _req
        resp = _req.get(
            "https://api.github.com/copilot_internal/v2/token",
            headers={
                "Authorization": f"token {self._oauth_token}",
                "Accept": "application/json",
                "Editor-Version": "vscode/1.99.0",
                "Copilot-Integration-Id": "vscode-chat",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        AIAgent._session = {"token": data["token"], "expires_at": data.get("expires_at", now + 1800)}
        return AIAgent._session["token"]

    def _make_client(self) -> OpenAI:
        token = self._get_session_token()
        return OpenAI(
            api_key=token,
            base_url=self._base_url,
            default_headers={
                "Editor-Version": "vscode/1.99.0",
                "Editor-Plugin-Version": "copilot-chat/0.26.0",
                "Copilot-Integration-Id": "vscode-chat",
                "OpenAI-Intent": "conversation-panel",
            },
        )

    # ------------------------------------------------------------------
    # Phase 2 – PR description
    # ------------------------------------------------------------------

    def generate_pr_description(self, issue: dict[str, Any]) -> str:
        """Return a markdown PR description for the given Jira issue."""
        messages = self._prompt_builder.build_pr_description_messages(issue)
        response = self._chat(messages, max_tokens=600)
        logger.info("Generated PR description for %s", issue.get("key"))
        return response

    # ------------------------------------------------------------------
    # Phase 3 – Code changes
    # ------------------------------------------------------------------

    def generate_code_changes(
        self,
        issue: dict[str, Any],
        readme: str,
        relevant_files: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """
        Ask the model to implement the issue.
        Returns a list of ``{"path": ..., "content": ...}`` dicts.
        On any parse error an empty list is returned (fail-safe).
        """
        messages = self._prompt_builder.build_code_gen_messages(issue, readme, relevant_files)
        raw = self._chat(messages, max_tokens=4096)
        changes = _parse_json_changes(raw, issue.get("key", ""))
        logger.info(
            "Generated %d file change(s) for %s", len(changes), issue.get("key")
        )
        return changes

    # ------------------------------------------------------------------
    # Phase 4 – Test generation
    # ------------------------------------------------------------------

    def generate_tests(
        self,
        issue: dict[str, Any],
        changed_files: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """
        Generate unit tests for the changed files.
        Returns a list of ``{"path": ..., "content": ...}`` dicts.
        """
        if not changed_files:
            return []
        messages = self._prompt_builder.build_test_gen_messages(issue, changed_files)
        raw = self._chat(messages, max_tokens=2048)
        tests = _parse_json_changes(raw, issue.get("key", "") + "-tests")
        logger.info(
            "Generated %d test file(s) for %s", len(tests), issue.get("key")
        )
        return tests

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _chat(self, messages: list[dict[str, str]], max_tokens: int = 1024) -> str:
        client = self._make_client()
        completion = client.chat.completions.create(
            model=self._model,
            messages=messages,  # type: ignore[arg-type]
            max_tokens=max_tokens,
            temperature=0.2,
        )
        return completion.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json_changes(raw: str, label: str) -> list[dict[str, str]]:
    """
    Try to parse the AI response as a JSON array of file-change dicts.
    Returns an empty list if parsing fails.
    """
    # Strip common markdown code fences the model might add despite instructions
    text = raw.strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
    if text.endswith("```"):
        text = "\n".join(text.split("\n")[:-1])
    text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error(
            "Could not parse AI JSON response for %s: %s\nRaw (first 500 chars): %.500s",
            label,
            exc,
            raw,
        )
        return []

    if not isinstance(data, list):
        logger.error("AI response for %s is not a JSON array", label)
        return []

    validated: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        path = item.get("path", "").strip()
        content = item.get("content", "")
        if not path:
            continue
        validated.append({"path": path, "content": content})

    return validated
