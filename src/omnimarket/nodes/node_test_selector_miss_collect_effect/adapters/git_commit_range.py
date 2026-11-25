# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The first-parent commits between two nightly heads, read from a local clone (read-only git)."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
    ModelRangeCommit,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.ports import (
    GatewayReadError,
)

_TIMEOUT_S = 60
_SHA = frozenset("0123456789abcdef")


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


class GitCommitRange:
    """``ProtocolCommitRange`` over ``git log --first-parent`` in one clone. Never fetches or writes."""

    def __init__(self, clone: Path) -> None:
        self._clone = clone

    def first_parent(
        self, previous_head: str, head: str
    ) -> tuple[ModelRangeCommit, ...]:
        for sha in (previous_head, head):
            if not sha or not set(sha) <= _SHA:
                raise GatewayReadError(f"not a commit id: {sha!r}")
        try:
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self._clone),
                    "log",
                    "--first-parent",
                    "--reverse",
                    "--format=%H%x1f%cI",
                    f"{previous_head}..{head}",
                ],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_S,
                env=_git_env(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GatewayReadError(f"git log failed: {exc}") from exc
        if proc.returncode != 0:
            raise GatewayReadError(f"git log failed: {proc.stderr.strip()[-300:]}")
        commits: list[ModelRangeCommit] = []
        for line in proc.stdout.splitlines():
            sha, _, stamp = line.partition("\x1f")
            commits.append(
                ModelRangeCommit(sha=sha, committed_at=datetime.fromisoformat(stamp))
            )
        return tuple(commits)


__all__: list[str] = ["GitCommitRange"]
