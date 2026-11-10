# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read repositories through git at a ref; the working tree is never read."""

from __future__ import annotations

import subprocess
from pathlib import Path

from omnimarket.nodes.node_validator_estate_reading_effect.protocols import (
    EstateSourceError,
)


class GitEstateSource:
    def __init__(self, root: Path, timeout_seconds: int) -> None:
        self._root = root
        self._timeout = timeout_seconds

    def _git(self, repo: str, *args: str) -> subprocess.CompletedProcess[str]:
        clone = self._root / repo
        try:
            return subprocess.run(
                ["git", "-C", str(clone), *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=self._timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EstateSourceError(f"{repo}: {type(exc).__name__}") from exc

    def head(self, repo: str, ref: str) -> str:
        done = self._git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
        if done.returncode != 0:
            raise EstateSourceError(f"{repo}: ref {ref} not readable")
        return done.stdout.strip()

    def files_containing(
        self, repo: str, ref: str, pattern: str, roots: tuple[str, ...]
    ) -> tuple[str, ...]:
        done = self._git(repo, "grep", "-l", "-E", pattern, ref, "--", *roots)
        if done.returncode == 1:
            return ()
        if done.returncode != 0:
            raise EstateSourceError(f"{repo}: git grep exit {done.returncode}")
        prefix = f"{ref}:"
        return tuple(
            line.removeprefix(prefix)
            for line in done.stdout.splitlines()
            if line.endswith(".py")
        )

    def read(self, repo: str, ref: str, path: str) -> str | None:
        done = self._git(repo, "show", f"{ref}:{path}")
        return done.stdout if done.returncode == 0 else None

    def commits_citing(self, repo: str, ref: str, ticket: str) -> int:
        done = self._git(repo, "log", ref, "--oneline", f"--grep={ticket}\\b", "-E")
        if done.returncode != 0:
            raise EstateSourceError(f"{repo}: git log exit {done.returncode}")
        return len([line for line in done.stdout.splitlines() if line.strip()])
