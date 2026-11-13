# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the closer's pull request rows are read from (OMN-20655).

The in-memory source is the default; the clone source reads each listed repository's own
default branch with one ``git log``, and the watcher state is read from its file with the
schema and freshness the overlay names. A source that cannot answer says why: an unread clone
is left out of the answer (the rules name it), an unread or stale watcher state is a reason,
never an empty open set.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.events.closeout_scope_overlay import (
    EnumCloneLocation,
    ModelCloseoutScopeOverlay,
    ModelPrRepository,
)
from omnimarket.handlers.closeout_boundary_reads import read_text_file

_STAMP = "%Y-%m-%dT%H:%M:%SZ"
WATCHER_STATE_ENV = "ONEX_PR_WATCHER_STATE"


class ProtocolMergeSubjects(Protocol):
    def subjects(self) -> dict[str, list[str]]:
        """Repository name -> squash-merge subjects on its default branch; an unreadable
        repository is absent."""
        ...


class InMemoryMergeSubjects:
    def __init__(self, by_repo: Mapping[str, list[str]] | None = None) -> None:
        self._by_repo = {k: list(v) for k, v in (by_repo or {}).items()}

    def subjects(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self._by_repo.items()}


def clone_path(home: Path, repository: ModelPrRepository) -> Path:
    if repository.location is EnumCloneLocation.REGISTRY_ROOT:
        return home
    if repository.location is EnumCloneLocation.BESIDE_REGISTRY:
        return home.parent / repository.name
    return home / repository.name


class GitCloneMergeSubjects:
    """The whole history of ``origin/<default branch>`` of each listed repository.

    A repository's merges are read on the branch the overlay names for it and on no other: a
    clone without that branch is unread, not read from a different one.
    """

    def __init__(self, home: Path, overlay: ModelCloseoutScopeOverlay) -> None:
        self._home = home
        self._overlay = overlay

    @staticmethod
    def _git(clone: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(clone), *args],
            capture_output=True,
            text=True,
            check=False,
            env=scrub_git_location_env(os.environ),
        )

    def subjects(self) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for repository in self._overlay.pr_repositories:
            clone = clone_path(self._home, repository)
            if not clone.is_dir():
                continue
            ref = f"origin/{repository.default_branch}"
            if self._git(clone, "rev-parse", "-q", "--verify", ref).returncode != 0:
                continue
            log = self._git(clone, "log", ref, "--format=%s")
            if log.returncode == 0:
                found[repository.name] = log.stdout.splitlines()
        return found


def watcher_state_path(home: Path, environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    named = env.get(WATCHER_STATE_ENV, "")
    return (
        Path(named) if named else home / ".claude_scratch" / "pr-watcher" / "state.json"
    )


def _stamp(value: object) -> datetime | None:
    try:
        return datetime.strptime(str(value), _STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None


def read_watcher_state(
    path: Path, now: datetime, overlay: ModelCloseoutScopeOverlay
) -> tuple[dict[str, Any] | None, str]:
    """The state and an empty reason, or None and why it cannot be read as current."""
    try:
        raw = json.loads(read_text_file(path))
    except (OSError, ValueError) as exc:
        return None, f"PR watcher state unreadable: {path} is not readable: {exc}"
    schema = overlay.watcher_state_schema
    if not isinstance(raw, dict) or raw.get("schema") != schema:
        return None, f"PR watcher state unreadable: {path} is not schema {schema}"
    if not isinstance(raw.get("repos"), dict) or not isinstance(raw.get("prs"), dict):
        return None, f"PR watcher state unreadable: {path} carries no repos or prs"
    bounds = (
        ("last_tick", timedelta(seconds=overlay.watcher_tick_max_age_seconds)),
        ("last_full_resync", timedelta(seconds=overlay.watcher_resync_max_age_seconds)),
    )
    for field, bound in bounds:
        at = _stamp(raw.get(field))
        if at is None:
            return None, f"PR watcher state unreadable: {path} has no readable {field}"
        if now - at > bound:
            return (
                None,
                f"PR watcher state unreadable: {path} is stale: {field} "
                f"{raw.get(field)} is older than {bound}",
            )
    return raw, ""


def wait_for_watcher_state(
    path: Path,
    overlay: ModelCloseoutScopeOverlay,
    *,
    wait_s: float,
    deadline: datetime | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Re-read only the local projection, bounded by the wait, the overlay's wait and the run's
    deadline. Monotonic time bounds the wait even if UTC moves."""
    budget = min(float(overlay.watcher_wait_seconds), max(0.0, wait_s))
    if deadline is not None:
        budget = min(budget, max(0.0, (deadline - datetime.now(UTC)).total_seconds()))
    end = time.monotonic() + budget
    while True:
        now = datetime.now(UTC)
        state, why = read_watcher_state(path, now, overlay)
        remaining = end - time.monotonic()
        if deadline is not None:
            remaining = min(remaining, (deadline - now).total_seconds())
        if state is not None or remaining <= 0:
            return state, why
        time.sleep(min(1.0, remaining))
