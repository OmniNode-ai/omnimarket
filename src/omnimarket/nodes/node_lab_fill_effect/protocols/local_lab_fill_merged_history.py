# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The merges the canonical clones' default branches name for a ticket, read with git and never from GitHub (OMN-23376)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.handlers.rules_lab_fill_closer import merges_from_clone_log

from ..models.model_lab_fill_merged_history import (
    ModelLabFillMergedHistory,
    load_merged_history,
)
from .protocol_lab_fill_effect import LabFillPortError

_GIT_TIMEOUT_S = 20


class LocalMergedHistory:
    """``git log <default ref> --grep <ticket>`` on every clone the settings list that this host holds."""

    def __init__(
        self,
        settings: ModelLabFillMergedHistory | None = None,
        root: Path | None = None,
    ) -> None:
        self._settings = settings if settings is not None else load_merged_history()
        self._root = root

    def _clones(self) -> dict[str, Path]:
        base = self._root if self._root is not None else Path(os.environ["OMNI_HOME"])
        found: dict[str, Path] = {}
        for name in self._settings.repos:
            for candidate in (base / name, base.parent / name):
                if (candidate / ".git").exists():
                    found[name] = candidate
                    break
        return found

    def merged(self, ticket: str) -> tuple[str, ...]:
        """The pull requests (``repo#number``, or ``repo@sha``) that name the ticket on any readable clone.

        Raises ``LabFillPortError`` when no clone could be read at all: unread is never clear."""
        clones = self._clones()
        read = 0
        names: set[str] = set()
        for name, path in clones.items():
            ref = next(
                (
                    ref
                    for ref in self._settings.base_refs
                    if self._git(
                        path, "rev-parse", "--verify", "--quiet", ref + "^{commit}"
                    )
                    is not None
                ),
                None,
            )
            if ref is None:
                continue
            log = self._git(
                path,
                "log",
                ref,
                f"--since={self._settings.since_days} days ago",
                "--fixed-strings",
                f"--grep={ticket}",
                "--format=%H%x09%s",
            )
            if log is None:
                continue
            read += 1
            names.update(
                merges_from_clone_log(
                    name,
                    log,
                    [ticket],
                    evidence_prefix=self._settings.evidence_prefix,
                ).get(ticket, ())
            )
        if not read:
            raise LabFillPortError("merged history unreadable: no clone could be read")
        return tuple(sorted(names))

    @staticmethod
    def _git(path: Path, *args: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", "-C", str(path), *args],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_S,
                check=False,
                env=scrub_git_location_env(os.environ),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None
