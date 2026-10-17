# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads a file at a commit from a git checkout (OMN-17427)."""

from __future__ import annotations

import re
import subprocess
from pathlib import PurePosixPath

_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_GIT_TIMEOUT_SECONDS = 60


class GitHeadFileReader:
    """``git show <sha>:<path>`` in one checkout.

    The reviewer's quorum lower-cases the path it reports, so a path that is
    absent as written is matched case-insensitively against the commit's tree,
    and used only when exactly one file matches.
    """

    def __init__(self, repo_root: str) -> None:
        self._repo_root = repo_root
        self._tree: dict[str, dict[str, list[str]]] = {}

    def read(self, head_sha: str, path: str) -> str | None:
        if not _SHA.match(head_sha):
            return None
        posix = PurePosixPath(path)
        if posix.is_absolute() or ".." in posix.parts or not posix.parts:
            return None
        text = self._show(head_sha, str(posix))
        if text is not None:
            return text
        matches = self._names(head_sha).get(str(posix).lower(), [])
        if len(matches) != 1:
            return None
        return self._show(head_sha, matches[0])

    def _show(self, head_sha: str, path: str) -> str | None:
        proc = subprocess.run(
            ["git", "-C", self._repo_root, "show", f"{head_sha}:{path}"],
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            return None
        try:
            return proc.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def _names(self, head_sha: str) -> dict[str, list[str]]:
        if head_sha not in self._tree:
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    self._repo_root,
                    "ls-tree",
                    "-r",
                    "--name-only",
                    head_sha,
                ],
                capture_output=True,
                check=False,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
            by_lower: dict[str, list[str]] = {}
            if proc.returncode == 0:
                for name in proc.stdout.decode("utf-8", errors="replace").splitlines():
                    by_lower.setdefault(name.lower(), []).append(name)
            self._tree[head_sha] = by_lower
        return self._tree[head_sha]


__all__: list[str] = ["GitHeadFileReader"]
