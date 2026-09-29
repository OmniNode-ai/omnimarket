# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bounded subprocess boundaries: git, the model decider and the pin command."""

import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

from omnimarket.events.worktree_reconcile import (
    ModelWorktreeDecisionRecord,
    ModelWorktreeFacts,
)


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def git(
    path: str, *args: str, ok: tuple[int, ...] = (0,), timeout: float = 60
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", path, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode not in ok:
        # stderr may contain credential-bearing URLs; never send it to a model.
        raise RuntimeError(f"git {args[0]} failed ({proc.returncode})")
    return proc


# A model call per ambiguous row; the budget grows with the batch, capped.
DECIDER_SECONDS_PER_ROW = 240
DECIDER_MAX_SECONDS = 3 * 3600
# A pin runs the backup scan and a push: minutes, not seconds.
PIN_TIMEOUT_SECONDS = 1200
_BRANCH_UNSAFE = re.compile(r"[^A-Za-z0-9._/-]+")


class CommandWorktreeDecider:
    def decide(
        self, argv: list[str], facts: tuple[ModelWorktreeFacts, ...]
    ) -> tuple[ModelWorktreeDecisionRecord, ...]:
        payload = TypeAdapter(tuple[ModelWorktreeFacts, ...]).dump_json(facts).decode()
        budget = min(
            DECIDER_MAX_SECONDS, max(300, DECIDER_SECONDS_PER_ROW * len(facts))
        )
        proc = subprocess.run(
            argv,
            input=payload,
            capture_output=True,
            text=True,
            timeout=budget,
            check=True,
        )
        return TypeAdapter(tuple[ModelWorktreeDecisionRecord, ...]).validate_json(
            proc.stdout
        )


def backup_branch(facts: ModelWorktreeFacts) -> str:
    """The backup branch a pin pushes: host and the tree's path under its root."""
    relative = Path(facts.path).relative_to(Path(facts.root)).as_posix()
    tail = _BRANCH_UNSAFE.sub("-", f"{facts.host}/{relative}").strip("-/.")
    return f"backup/worktree-reconcile/{tail}"


class CommandWorktreePinner:
    """Run the configured pin command, then prove the pin on the remote itself.

    The command's own report is not trusted: success is ``origin`` serving the
    backup branch at exactly this tree's HEAD, read back with ``git ls-remote``.
    """

    def pin(self, argv: list[str], facts: ModelWorktreeFacts) -> bool:
        branch = backup_branch(facts)
        args = [
            arg.replace("{path}", facts.path).replace("{branch}", branch)
            for arg in argv
        ]
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=PIN_TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0:
            return False
        ref = f"refs/heads/{branch}"
        output = git(
            facts.path, "ls-remote", "--exit-code", "origin", ref, ok=(0, 2)
        ).stdout
        return any(
            line.split() == [facts.head_sha, ref] for line in output.splitlines()
        )
