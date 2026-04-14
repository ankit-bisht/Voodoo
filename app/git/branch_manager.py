from __future__ import annotations

import logging
import re

import git
from github import Github

from app.config.settings import settings

logger = logging.getLogger(__name__)


def _slugify(text: str, max_len: int = 50) -> str:
    """Convert free text to a URL/branch-safe slug."""
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:max_len].rstrip("-")


class BranchManager:
    """Creates and pushes a feature branch for a Jira issue."""

    def __init__(self) -> None:
        self._repo_path = settings.repo_local_path
        self._base_branch = settings.base_branch
        self._github = Github(settings.github_token)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_branch(self, issue_key: str, title: str) -> str:
        """
        Checkout a new branch from *base_branch*, push it, and return the
        branch name.
        """
        branch_name = f"feature/{issue_key}-{_slugify(title)}"
        repo = git.Repo(self._repo_path)

        # Ensure we have the latest base branch
        origin = repo.remotes.origin
        origin.fetch()

        base_ref = f"origin/{self._base_branch}"
        branch = repo.create_head(branch_name, base_ref)
        branch.checkout()
        logger.info("Checked out new branch %s", branch_name)

        # Push to remote (set upstream)
        origin.push(refspec=f"{branch_name}:{branch_name}", set_upstream=True)
        logger.info("Pushed branch %s to origin", branch_name)

        return branch_name

    def get_current_branch(self) -> str:
        repo = git.Repo(self._repo_path)
        return repo.active_branch.name
