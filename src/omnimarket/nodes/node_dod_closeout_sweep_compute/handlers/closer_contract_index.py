# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which tickets have a contract on a product default branch (OMN-20655).

The in-memory index is the default; the clone index reads ``contracts/`` on each overlay
repository's default branch with one ``git ls-tree`` per repository.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.models.closeout_scope_overlay import (
    ModelCloseoutScopeOverlay,
)

_CONTRACT_FILE = re.compile(r"^contracts/(OMN-\d+)\.yaml$")


class ProtocolContractIndex(Protocol):
    def contract_ids(self) -> frozenset[str]:
        """The ticket ids that have contracts/<id>.yaml on a product default branch."""
        ...


class InMemoryContractIndex:
    def __init__(self, ids: frozenset[str] = frozenset()) -> None:
        self._ids = ids

    def contract_ids(self) -> frozenset[str]:
        return self._ids


class GitCloneContractIndex:
    """Every git clone directly under the registry root, the root itself and each extra
    clone root; the overlay may restrict the search to clones it names."""

    def __init__(
        self,
        home: Path,
        overlay: ModelCloseoutScopeOverlay,
        extra_roots: Sequence[Path] = (),
    ) -> None:
        self._home = home
        self._overlay = overlay
        self._extra = tuple(extra_roots)

    def _clones(self) -> list[Path]:
        named = set(self._overlay.contract_repos)
        children = sorted(
            c
            for c in self._home.iterdir()
            if c.is_dir() and (c / ".git").exists() and (not named or c.name in named)
        )
        return [self._home, *children, *self._extra]

    @staticmethod
    def _git(clone: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(clone), *args],
            capture_output=True,
            text=True,
            check=False,
            env=scrub_git_location_env(os.environ),
        )

    def contract_ids(self) -> frozenset[str]:
        found: set[str] = set()
        for clone in self._clones():
            if not (clone / ".git").exists():
                continue
            ref = next(
                (
                    r
                    for r in self._overlay.default_refs
                    if self._git(clone, "rev-parse", "-q", "--verify", r).returncode
                    == 0
                ),
                "",
            )
            if not ref:
                continue
            listing = self._git(clone, "ls-tree", "--name-only", ref, "contracts/")
            if listing.returncode != 0:
                continue
            for line in listing.stdout.splitlines():
                match = _CONTRACT_FILE.match(line.strip())
                if match:
                    found.add(match.group(1))
        return frozenset(found)
