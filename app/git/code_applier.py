from __future__ import annotations

import logging
import os
from typing import Any

import git

from app.config.settings import settings

logger = logging.getLogger(__name__)


class CodeApplier:
    """Writes AI-generated file changes to disk, commits, and pushes."""

    def __init__(self) -> None:
        self._repo_path = settings.repo_local_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply_changes(
        self,
        changes: list[dict[str, str]],
        commit_message: str,
    ) -> bool:
        """
        Write each ``{"path": ..., "content": ...}`` entry to disk,
        stage, commit, and push.

        Returns True on success.
        """
        if not changes:
            logger.warning("apply_changes called with empty change list")
            return False

        repo = git.Repo(self._repo_path)

        written: list[str] = []
        for change in changes:
            rel_path: str = change.get("path", "").lstrip("/")
            content: str = change.get("content", "")
            if not rel_path:
                logger.warning("Skipping change with empty path")
                continue

            abs_path = os.path.join(self._repo_path, rel_path)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)

            with open(abs_path, "w", encoding="utf-8") as fh:
                fh.write(content)

            written.append(rel_path)
            logger.info("Wrote %s", rel_path)

        if not written:
            logger.warning("No files were written")
            return False

        repo.index.add(written)
        repo.index.commit(commit_message)
        origin = repo.remotes.origin
        origin.push()
        logger.info("Committed and pushed: %s", commit_message)
        return True

    def commit_exists(self, message_prefix: str) -> bool:
        """Return True if the HEAD commit message starts with *message_prefix*."""
        repo = git.Repo(self._repo_path)
        return repo.head.commit.message.startswith(message_prefix)
