# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The open pull requests of the registry, read from the PR watcher's state (OMN-20911).

This used to run ``gh pr list`` once per repository. Every closeout, lab-fill and morning run that
derived the fence list therefore spent 14 GitHub reads at its first step, and a second derivation
in the same run found the host's lane read budget spent (measured 2026-10-13: the 14:45Z closeout
made 14 calls at 14:45:23 to 14:45:30, its two re-runs were refused). The watcher already holds
every open PR (RULING 2026-09-27T18:05:21Z: PR-state reads come from its state file), so the fence
list reads that file and makes no GitHub call.

A state that is missing, unparseable, of another schema, older than the freshness bounds below, or
silent about a repository is an error for that repository, never an empty list and never a fallback
read from GitHub: a repository whose lookup fails records an error and the sweep continues, because
an unreadable source must never blank the fence list without saying so.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# One repository's open PRs as the rows the fence derivation reads: number, title, headRefName,
# updatedAt, url.
PullReader = Callable[[str], list[dict[str, Any]]]

GITHUB_ORG = "OmniNode-ai"
STATE_SCHEMA = 1
STATE_ENV = "ONEX_PR_WATCHER_STATE"
# The bounds ``pr_state_local.py`` applies: one watcher interval (300 s) plus a tick of up to about
# two minutes plus margin, and the full resync that re-lists every open PR.
MAX_TICK_AGE = timedelta(minutes=8)
MAX_RESYNC_AGE = timedelta(minutes=90)
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


class WatcherStateUnreadableError(Exception):
    """The watcher's state cannot answer; the message names why."""


def _stamp(value: object) -> datetime | None:
    try:
        return datetime.strptime(str(value), _STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None


def state_path(environ: Mapping[str, str] | None = None) -> Path:
    """``$ONEX_PR_WATCHER_STATE``, else ``$OMNI_HOME/.claude_scratch/pr-watcher/state.json``."""
    env = os.environ if environ is None else environ
    override = env.get(STATE_ENV)
    if override:
        return Path(override)
    home = env.get("OMNI_HOME")
    if not home:
        raise WatcherStateUnreadableError(f"neither {STATE_ENV} nor OMNI_HOME is set")
    return Path(home) / ".claude_scratch" / "pr-watcher" / "state.json"


class WatcherStatePulls:
    """Reads one repository's open PRs from the watcher's state file, loaded once per run."""

    def __init__(self, *, now: datetime, path: Path | None = None) -> None:
        self._now = now
        self._path = path
        self._state: dict[str, Any] | None = None
        self._why = ""

    def read(self) -> dict[str, Any]:
        """Read the same validated snapshot for fence and closeout consumers."""
        if self._state is not None:
            return self._state
        if self._why:
            raise WatcherStateUnreadableError(self._why)
        try:
            self._state = self._read()
        except WatcherStateUnreadableError as exc:
            self._why = str(exc)
            raise
        return self._state

    def _read(self) -> dict[str, Any]:
        path = self._path or state_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WatcherStateUnreadableError(
                f"watcher state {path} is not readable: {exc}"
            ) from exc
        if not isinstance(raw, dict) or raw.get("schema") != STATE_SCHEMA:
            raise WatcherStateUnreadableError(
                f"watcher state {path} is not schema {STATE_SCHEMA}"
            )
        if not isinstance(raw.get("repos"), dict) or not isinstance(
            raw.get("prs"), dict
        ):
            raise WatcherStateUnreadableError(
                f"watcher state {path} carries no repos or prs"
            )
        for field, bound in (
            ("last_tick", MAX_TICK_AGE),
            ("last_full_resync", MAX_RESYNC_AGE),
        ):
            at = _stamp(raw.get(field))
            if at is None:
                raise WatcherStateUnreadableError(
                    f"watcher state {path} has no readable {field}"
                )
            if self._now - at > bound:
                raise WatcherStateUnreadableError(
                    f"watcher state {path} is stale: {field} {raw.get(field)} is older than {bound}"
                )
        return raw

    def __call__(self, repo: str) -> list[dict[str, Any]]:
        # Only the fence adapter renders URLs; the snapshot reader also runs
        # under the closer's bare python3 and needs no model dependencies.

        state = self.read()
        if repo not in state["repos"]:
            raise WatcherStateUnreadableError(
                f"{repo} is not a repository the watcher state lists"
            )
        rows: list[dict[str, Any]] = []
        for record in state["prs"].values():
            facts = record.get("facts") if isinstance(record, dict) else None
            if not isinstance(facts, dict):
                continue
            if facts.get("repo") != repo or facts.get("state") != "OPEN":
                continue
            number = facts.get("number")
            rows.append(
                {
                    "number": number,
                    "title": facts.get("title", ""),
                    "headRefName": facts.get("head_ref", ""),
                    "updatedAt": facts.get("updated_at", ""),
                    "url": f"https://github.com/{GITHUB_ORG}/{repo}/pull/{number}",
                }
            )
        return sorted(rows, key=lambda row: int(row["number"]))


def read_open_pulls(
    *,
    repos: Sequence[str],
    reader: PullReader,
    errors: list[str] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Every open PR of every repo as (repo, row), read through ``reader``.

    Best-effort by construction: a repo whose lookup fails records an error and the sweep
    continues, so an unreadable source is reported and never blanks the ledger fences.
    """
    collected: list[tuple[str, dict[str, Any]]] = []
    for repo in repos:
        try:
            payload = reader(repo)
        except Exception as exc:
            if errors is not None:
                errors.append(f"open-pr lookup failed for {repo}: {exc}")
            continue
        collected.extend((repo, pull) for pull in payload)
    return collected
