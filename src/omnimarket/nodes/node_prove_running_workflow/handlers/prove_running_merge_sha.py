# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The merge sha of a merged change, read through ``onex scm pr-merge-commit`` (the PR watcher state and the clones)."""

from __future__ import annotations

import json
import subprocess


def _is_sha(token: object) -> bool:
    return (
        isinstance(token, str)
        and len(token) == 40
        and all(c in "0123456789abcdef" for c in token)
    )


def parse_merge_sha(stdout: str) -> str | None:
    """The merge sha in ``onex scm pr-merge-commit`` output: the verb's JSON line (``result.merge_commit_sha``), or a
    bare 40-hex token on an older verb that printed one."""
    for line in reversed(stdout.splitlines()):
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        result = doc.get("result") if isinstance(doc, dict) else None
        sha = result.get("merge_commit_sha") if isinstance(result, dict) else None
        if _is_sha(sha):
            return str(sha)
    for token in reversed(stdout.split()):
        if _is_sha(token):
            return token
    return None


class CliMergeShaResolver:
    """The merge sha through ``onex scm pr-merge-commit`` (the PR watcher state and the clones, never GitHub)."""

    def __init__(self, owner: str, override_argv: tuple[str, ...] = ()) -> None:
        self._owner = owner
        self._override_argv = override_argv

    def merge_sha(self, repository: str, number: int) -> str | None:
        change_key = f"{repository}#{number}"
        argv = (
            [*self._override_argv, change_key]
            if self._override_argv
            else ["onex", "scm", "pr-merge-commit", f"{self._owner}/{change_key}"]
        )
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
        if self._override_argv:
            lines = proc.stdout.strip().splitlines()
            return lines[-1].strip() if proc.returncode == 0 and lines else None
        return parse_merge_sha(proc.stdout)
