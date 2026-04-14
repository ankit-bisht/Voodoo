from __future__ import annotations

import logging

from github import Github
from github.PullRequest import PullRequest

from app.config.settings import settings

logger = logging.getLogger(__name__)

# ── MERGE HARD STOP ───────────────────────────────────────────────────────────
# This agent NEVER merges PRs. The methods below are intentionally overridden
# to raise immediately so no future code path can accidentally merge.


def _MERGE_FORBIDDEN(*args, **kwargs):  # noqa: N802, ARG001
    raise RuntimeError(
        "HARD STOP: Merging a PR is strictly forbidden. "
        "This agent only creates branches and opens PRs. "
        "A human must review and merge manually."
    )


# Monkey-patch PyGithub's PullRequest.merge so it can never be called
PullRequest.merge = _MERGE_FORBIDDEN  # type: ignore[method-assign]


class PRCreator:
    """Opens Pull Requests on GitHub using PyGithub."""

    def __init__(self) -> None:
        self._gh = Github(settings.github_token)
        self._repo = self._gh.get_repo(settings.github_repo)
        self._base_branch = settings.base_branch

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_pr(
        self,
        branch_name: str,
        title: str,
        body: str = "",
        draft: bool = True,
    ) -> PullRequest:
        """
        Create a (draft) pull request from *branch_name* → *base_branch*.
        Returns the created PR object.
        """
        pr = self._repo.create_pull(
            title=title,
            body=body or "_PR auto-created by Jira AI Agent. AI-generated description will be added shortly._",
            head=branch_name,
            base=self._base_branch,
            draft=draft,
        )
        logger.info("Created PR #%s: %s", pr.number, pr.html_url)
        return pr

    def update_pr_body(self, pr: PullRequest, body: str) -> None:
        """Replace the PR description with *body*."""
        pr.edit(body=body)
        logger.info("Updated PR #%s description", pr.number)
