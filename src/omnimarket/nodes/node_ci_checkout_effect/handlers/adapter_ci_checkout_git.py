# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Git and clone-sync adapters of node_ci_checkout_effect."""

from __future__ import annotations

import subprocess
from collections.abc import Callable


class SubprocessCheckoutGit:
    """Answers has_commit with ``git -C <path> cat-file -e <sha>^{commit}``."""

    def __init__(self, timeout_seconds: float) -> None:
        self._timeout_seconds = timeout_seconds

    def has_commit(self, repo_path: str, sha: str) -> bool:
        argv = ["git", "-C", repo_path, "cat-file", "-e", f"{sha}^{{commit}}"]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                timeout=self._timeout_seconds,
                check=False,
            )
            return completed.returncode == 0
        except subprocess.TimeoutExpired:
            return False


class CommandCloneSync:
    """Runs the overlay's sync command template, formatting {repo} and {ref}."""

    def __init__(
        self, command_template: tuple[str, ...], timeout_seconds: float
    ) -> None:
        self._command_template = command_template
        self._timeout_seconds = timeout_seconds

    def sync(self, repo: str, ref: str | None) -> bool:
        argv: list[str] = []
        for element in self._command_template:
            if element == "{ref}":
                if ref is None:
                    # A missing ref drops the {ref} element and the --branch flag
                    # directly preceding it.
                    if argv and argv[-1] == "--branch":
                        argv.pop()
                    continue
                argv.append(ref)
                continue
            argv.append(element.format(repo=repo, ref=ref or ""))
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                timeout=self._timeout_seconds,
                check=False,
            )
            return completed.returncode == 0
        except subprocess.TimeoutExpired:
            return False


class InMemoryCheckoutGit:
    """In-memory git source for tests and the in-memory profile."""

    def __init__(self, held: dict[str, set[str]]) -> None:
        self._held = held

    def has_commit(self, repo_path: str, sha: str) -> bool:
        return sha in self._held.get(repo_path, set())


class RecordingCloneSync:
    """Records sync calls for tests; optionally invokes a callback."""

    def __init__(
        self, on_sync: Callable[[str, str | None], None] | None = None
    ) -> None:
        self._on_sync = on_sync
        self.calls: list[tuple[str, str | None]] = []

    def sync(self, repo: str, ref: str | None) -> bool:
        self.calls.append((repo, ref))
        if self._on_sync is not None:
            self._on_sync(repo, ref)
        return True
