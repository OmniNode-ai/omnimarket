# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads the lines a commit range adds or changes from a git checkout (OMN-17427)."""

from __future__ import annotations

import re
import subprocess

_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_GIT_TIMEOUT_SECONDS = 120
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


class GitDiffReader:
    """``git diff -U0`` between two commits, as new-side line ranges per file.

    A hunk that only deletes lines covers the line after the deletion and the
    one before it, so a finding anchored at a removal still reads as inside the
    diff. Paths git has to quote are left out: a finding on one has no evidence.
    """

    def __init__(self, repo_root: str) -> None:
        self._repo_root = repo_root

    def changed_lines(
        self, old_sha: str, new_sha: str, *, from_merge_base: bool
    ) -> dict[str, tuple[tuple[int, int], ...]] | None:
        if not (_SHA.match(old_sha) and _SHA.match(new_sha)):
            return None
        span = f"{old_sha}...{new_sha}" if from_merge_base else f"{old_sha}..{new_sha}"
        proc = subprocess.run(
            [
                "git",
                "-C",
                self._repo_root,
                "diff",
                "-U0",
                "--no-color",
                "--no-ext-diff",
                "--no-renames",
                span,
                "--",
            ],
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            return None
        return parse_zero_context_diff(proc.stdout.decode("utf-8", errors="replace"))


def parse_zero_context_diff(diff: str) -> dict[str, tuple[tuple[int, int], ...]]:
    """New-side inclusive line ranges per path from ``git diff -U0`` output."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            current = target[2:] if target.startswith("b/") else None
            if current is not None:
                ranges.setdefault(current, [])
            continue
        if current is None:
            continue
        hunk = _HUNK.match(line)
        if hunk is None:
            continue
        start = int(hunk.group("start"))
        count = 1 if hunk.group("count") is None else int(hunk.group("count"))
        if count == 0:
            ranges[current].append((max(start, 1), start + 1))
        else:
            ranges[current].append((start, start + count - 1))
    return {path: tuple(found) for path, found in ranges.items()}


__all__: list[str] = ["GitDiffReader", "parse_zero_context_diff"]
