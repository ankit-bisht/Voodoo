from __future__ import annotations

import logging
import os
import re
from collections import Counter
from typing import Any

from app.config.settings import settings

logger = logging.getLogger(__name__)

# File extensions we're interested in reading
_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go",
    ".rb", ".php", ".cs", ".cpp", ".c", ".h", ".rs",
    ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg",
    ".sh", ".bash", ".md",
}

# Directories to always skip
_SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", "node_modules",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".tox",
}


class ContextBuilder:
    """Selects the most relevant files from the local repository for a given issue."""

    def __init__(self) -> None:
        self._repo_path = settings.repo_local_path
        self._max_files = settings.max_context_files
        self._max_file_chars = settings.max_file_chars
        self._max_readme_chars = settings.max_readme_chars

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_readme(self) -> str:
        """Return the truncated content of README.md (or empty string)."""
        for name in ("README.md", "README.rst", "README.txt", "readme.md"):
            path = os.path.join(self._repo_path, name)
            if os.path.isfile(path):
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    return fh.read()[: self._max_readme_chars]
        return ""

    def find_relevant_files(self, issue: dict[str, Any]) -> list[dict[str, str]]:
        """
        Score every file in the repo against the issue keywords and return
        the top-N as ``[{"path": rel_path, "content": text}, ...]``.
        """
        keywords = _extract_keywords(issue)
        if not keywords:
            logger.warning("No keywords extracted from issue %s", issue.get("key"))
            return []

        logger.info("Searching repo with keywords: %s", keywords)

        scored: list[tuple[float, str]] = []

        for rel_path, abs_path in _walk_repo(self._repo_path):
            score = _score_file(rel_path, abs_path, keywords)
            if score > 0:
                scored.append((score, rel_path))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[: self._max_files]

        result: list[dict[str, str]] = []
        for _, rel_path in top:
            abs_path = os.path.join(self._repo_path, rel_path)
            try:
                with open(abs_path, encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()[: self._max_file_chars]
                result.append({"path": rel_path, "content": content})
                logger.debug("Selected file %s", rel_path)
            except OSError:
                pass

        logger.info("Selected %d relevant files for %s", len(result), issue.get("key"))
        return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_keywords(issue: dict[str, Any]) -> list[str]:
    """Extract meaningful tokens from the issue text fields."""
    text = " ".join([
        issue.get("summary", ""),
        issue.get("description", ""),
        issue.get("acceptance_criteria", ""),
    ])
    # Lower-case, keep only alphanumeric tokens
    tokens = re.findall(r"[a-z][a-z0-9_]{2,}", text.lower())
    # Remove very common English stop-words
    STOP = {
        "the", "and", "for", "are", "with", "this", "that", "have",
        "from", "not", "you", "was", "but", "will", "its", "can",
        "all", "when", "also", "more", "they", "them", "been",
        "into", "which", "your", "should", "would", "could",
    }
    freq = Counter(t for t in tokens if t not in STOP)
    # Return top-20 most frequent tokens
    return [tok for tok, _ in freq.most_common(20)]


def _walk_repo(repo_path: str):
    """Yield (relative_path, absolute_path) for eligible files."""
    for root, dirs, files in os.walk(repo_path):
        # Prune skip dirs in-place
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _CODE_EXTENSIONS:
                continue
            abs_path = os.path.join(root, fname)
            rel_path = os.path.relpath(abs_path, repo_path)
            yield rel_path, abs_path


def _score_file(rel_path: str, abs_path: str, keywords: list[str]) -> float:
    """
    Return a relevance score for a file (higher = more relevant).
    - Filename matches score higher than content matches.
    """
    score = 0.0
    path_lower = rel_path.lower()

    # Filename / directory keyword hits score more heavily
    for kw in keywords:
        if kw in path_lower:
            score += 3.0

    # Content keyword hits
    try:
        with open(abs_path, encoding="utf-8", errors="ignore") as fh:
            content_lower = fh.read(4096).lower()  # only scan first 4 KB
        for kw in keywords:
            count = content_lower.count(kw)
            if count:
                score += min(count * 0.5, 5.0)  # cap per-keyword content contribution
    except OSError:
        pass

    return score
