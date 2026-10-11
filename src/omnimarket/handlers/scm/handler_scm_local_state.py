# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR and CI state reads for ``onex scm``, from local sources first (OMN-20913).

The PR watcher already reads every open PR and its CI on each tick and writes
the result to its state file (``$ONEX_PR_WATCHER_STATE``, else
``$OMNI_HOME/.claude_scratch/pr-watcher/state.json``). The canonical clones
hold every merged commit. A lane's PR and CI state read is answered from those
two before any bus call, with zero GitHub requests.

Freshness is the rule ``pr_state_local.py`` and node_dod_verify's local source
use: a MERGED or CLOSED record is terminal and is served at any watcher age;
anything else is served only while the watcher's ``last_tick`` is at most
eight minutes old. An unanswered read says why, and the caller asks the bus.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from pydantic import JsonValue

from omnimarket.models.scm import (
    EnumScmLocalRead,
    ModelScmLocalReadRequest,
    ModelScmLocalReadResult,
)

logger = logging.getLogger(__name__)

WATCHER_STATE_ENV: Final[str] = "ONEX_PR_WATCHER_STATE"
WATCHER_SCHEMA: Final[int] = 1
WATCHER_MAX_AGE: Final[timedelta] = timedelta(minutes=8)
_TERMINAL_STATES: Final[frozenset[str]] = frozenset({"MERGED", "CLOSED"})
# The facts a pr-view answer carries; the watcher's PR body is left out (it can
# be 64 KiB and `onex scm pr-view --source bus` returns it through pr_text).
_VIEW_FACTS: Final[tuple[str, ...]] = (
    "number",
    "title",
    "state",
    "draft",
    "armed",
    "author",
    "base",
    "head_ref",
    "head_sha",
    "labels",
    "created_at",
    "updated_at",
    "merged_at",
)


def default_watcher_state_path() -> Path | None:
    """``$ONEX_PR_WATCHER_STATE``, else the watcher's file under ``$OMNI_HOME``."""
    declared = os.environ.get(WATCHER_STATE_ENV)
    if declared:
        return Path(declared)
    registry_root = os.environ.get("OMNI_HOME")
    if not registry_root:
        return None
    return Path(registry_root) / ".claude_scratch" / "pr-watcher" / "state.json"


def default_clone_path(repo: str) -> Path | None:
    """``$OMNI_HOME/<repo name>`` when it is a git checkout."""
    registry_root = os.environ.get("OMNI_HOME")
    if not registry_root:
        return None
    clone = Path(registry_root) / repo.split("/", 1)[1]
    return clone if (clone / ".git").exists() else None


def _parse_stamp(text: JsonValue) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _git(clone: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(clone), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("git %s in %s failed: %s", args[0], clone, exc)
        return None
    return done.stdout if done.returncode == 0 else None


class HandlerScmLocalState:
    """Answer one PR or CI state read from the watcher state and the clone."""

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now

    def handle(self, request: ModelScmLocalReadRequest) -> ModelScmLocalReadResult:
        state = self._load(request.watcher_state_path)
        if isinstance(state, str):
            state_dict: dict[str, JsonValue] = {}
            why: str | None = state
        else:
            state_dict, why = state, None
        tick = state_dict.get("last_tick")
        tick_text = tick if isinstance(tick, str) else None
        fresh = why is None and self._fresh(tick)
        if why is None and not fresh:
            why = f"the watcher state is older than {WATCHER_MAX_AGE} (last_tick {tick_text})"

        if request.read is EnumScmLocalRead.PR_LIST:
            if why is not None:
                return self._unanswered(request, why)
            return self._list(request, state_dict, tick_text)

        short = request.repo.split("/", 1)[1]
        key = f"{short}#{request.pr_number}"
        record = self._dict(self._dict(state_dict.get("prs")).get(key))
        merge = self._dict(self._dict(state_dict.get("merges")).get(key))
        facts = self._dict(record.get("facts"))
        pr_state = facts.get("state") or ("MERGED" if merge else None)
        terminal = isinstance(pr_state, str) and pr_state in _TERMINAL_STATES

        if request.read is EnumScmLocalRead.PR_MERGE_COMMIT:
            return self._merge_commit(request, facts, merge, tick_text)
        if not facts:
            return self._unanswered(
                request, why or f"the watcher state holds no record of {key}"
            )
        if not terminal and not fresh:
            return self._unanswered(request, why or "the watcher state is stale")

        result: dict[str, JsonValue] = {
            "repo": request.repo,
            "number": request.pr_number,
        }
        if request.read is EnumScmLocalRead.PR_VIEW:
            result.update({name: facts.get(name) for name in _VIEW_FACTS})
            result["class"] = record.get("cls")
            result["reason"] = record.get("reason")
        elif request.read is EnumScmLocalRead.PR_STATUS:
            result.update(
                {
                    "state": pr_state,
                    "head_sha": facts.get("head_sha"),
                    "draft": facts.get("draft"),
                    "armed": facts.get("armed"),
                    "blocked": record.get("blocked"),
                    "class": record.get("cls"),
                    "reason": record.get("reason"),
                    "handoff": record.get("handoff"),
                }
            )
        else:
            ci = self._dict(record.get("ci"))
            if not ci:
                return self._unanswered(
                    request, f"the watcher has no CI read of {key} yet"
                )
            result.update(
                {
                    "head_sha": facts.get("head_sha"),
                    "class": record.get("cls"),
                    "read_at": ci.get("read_at"),
                    "red": ci.get("red"),
                    "pending": ci.get("pending"),
                    "cancelled": ci.get("cancelled"),
                    "red_facts": ci.get("red_facts"),
                    "runs": ci.get("runs"),
                    "detail": ci.get("detail"),
                }
            )
        return ModelScmLocalReadResult(
            read=request.read,
            repo=request.repo,
            pr_number=request.pr_number,
            answered=True,
            source="watcher",
            watcher_tick=tick_text,
            result=result,
        )

    # ------------------------------------------------------------------ reads

    def _list(
        self,
        request: ModelScmLocalReadRequest,
        state: dict[str, JsonValue],
        tick: str | None,
    ) -> ModelScmLocalReadResult:
        short = request.repo.split("/", 1)[1]
        prs: list[JsonValue] = []
        for key, value in sorted(self._dict(state.get("prs")).items()):
            if not key.startswith(f"{short}#"):
                continue
            facts = self._dict(self._dict(value).get("facts"))
            if facts.get("state") != "OPEN":
                continue
            prs.append(
                {
                    "number": facts.get("number"),
                    "title": facts.get("title"),
                    "draft": facts.get("draft"),
                    "head_sha": facts.get("head_sha"),
                    "head_ref": facts.get("head_ref"),
                    "class": self._dict(value).get("cls"),
                }
            )
        return ModelScmLocalReadResult(
            read=request.read,
            repo=request.repo,
            answered=True,
            source="watcher",
            watcher_tick=tick,
            result={"repo": request.repo, "count": len(prs), "prs": prs},
        )

    def _merge_commit(
        self,
        request: ModelScmLocalReadRequest,
        facts: dict[str, JsonValue],
        merge: dict[str, JsonValue],
        tick: str | None,
    ) -> ModelScmLocalReadResult:
        sha = merge.get("merge_sha")
        if isinstance(sha, str) and sha:
            return ModelScmLocalReadResult(
                read=request.read,
                repo=request.repo,
                pr_number=request.pr_number,
                answered=True,
                source="watcher",
                watcher_tick=tick,
                result={
                    "repo": request.repo,
                    "number": request.pr_number,
                    "merged": True,
                    "merge_commit_sha": sha,
                },
            )
        base = facts.get("base") or merge.get("base")
        squash = self._squash_commit(
            request, base if isinstance(base, str) and base else None
        )
        if squash is None:
            return self._unanswered(
                request,
                "neither the watcher nor the canonical clone names a merge commit",
            )
        return ModelScmLocalReadResult(
            read=request.read,
            repo=request.repo,
            pr_number=request.pr_number,
            answered=True,
            source="clone",
            result={
                "repo": request.repo,
                "number": request.pr_number,
                "merged": True,
                "merge_commit_sha": squash,
            },
        )

    def _squash_commit(
        self, request: ModelScmLocalReadRequest, base: str | None
    ) -> str | None:
        """The one first-parent commit on ``origin/<base>`` ending ``(#<n>)``."""
        if request.clone_path is None:
            return None
        clone = Path(request.clone_path)
        if base is None:
            head = _git(clone, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
            if head is None or not head.strip().startswith("origin/"):
                return None
            base = head.strip()[len("origin/") :]
        suffix = f"(#{request.pr_number})"
        out = _git(
            clone,
            "log",
            "--first-parent",
            "--fixed-strings",
            f"--grep={suffix}",
            "--format=%H%x09%s",
            f"refs/remotes/origin/{base}",
        )
        if out is None:
            return None
        shas = [
            line.split("\t", 1)[0]
            for line in out.splitlines()
            if "\t" in line and line.split("\t", 1)[1].rstrip().endswith(suffix)
        ]
        return shas[0] if len(shas) == 1 else None

    # ---------------------------------------------------------------- helpers

    def _load(self, path_text: str | None) -> dict[str, JsonValue] | str:
        if path_text is None:
            return "no PR watcher state file is known on this host"
        path = Path(path_text)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return f"the watcher state {path} is unreadable: {exc}"
        if not isinstance(raw, dict) or raw.get("schema") != WATCHER_SCHEMA:
            return f"the watcher state {path} is not schema {WATCHER_SCHEMA}"
        return raw

    def _fresh(self, tick: JsonValue) -> bool:
        last = _parse_stamp(tick)
        if last is None:
            return False
        return (self._now or datetime.now(UTC)) - last <= WATCHER_MAX_AGE

    @staticmethod
    def _dict(value: JsonValue | None) -> dict[str, JsonValue]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _unanswered(
        request: ModelScmLocalReadRequest, why: str
    ) -> ModelScmLocalReadResult:
        return ModelScmLocalReadResult(
            read=request.read,
            repo=request.repo,
            pr_number=request.pr_number,
            answered=False,
            unanswered_reason=why,
        )


__all__: list[str] = [
    "WATCHER_MAX_AGE",
    "HandlerScmLocalState",
    "default_clone_path",
    "default_watcher_state_path",
]
