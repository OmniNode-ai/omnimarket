# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real git workspace and shell runner of node_code_contract_score_effect (OMN-23626).

The git workspace makes a detached worktree of a registry clone under the overlay's scratch root; it never
touches the clone's own checkout. The runner runs one command through bash with the database environment scrubbed the
way the verifier's own check runner scrubs it (``scrub_database_environment``), reads pytest's summary the way it
does (``command_failure``), and treats the wall-clock limit as a limit, never as an exit code.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path

from omnimarket.nodes.node_code_contract_score_effect.protocols import (
    RunOutcome,
    WorkspaceError,
)
from omnimarket.nodes.node_dod_verify.services.behavior_check_execution import (
    scrub_database_environment,
)

_GIT_TIMEOUT_S = 120
_GIT_LOCATION_ENV = frozenset(
    {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"}
)


def _git_env() -> dict[str, str]:
    """The environment git runs in: no inherited location variable can retarget a registry clone (OMN-14891)."""
    scrubbed = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_ENV}
    scrubbed["GIT_CONFIG_NOSYSTEM"] = "1"
    return scrubbed


def _git(
    cwd: str | Path, *args: str, stdin: str | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        input=None if stdin is None else stdin.encode(),
        capture_output=True,
        env=_git_env(),
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )


class GitWorkspace:
    """ProtocolCodeScoreWorkspace over the git CLI."""

    def read_blob(self, repo_root: str, revision: str, path: str) -> bytes | None:
        done = _git(repo_root, "show", f"{revision}:{path}")
        return done.stdout if done.returncode == 0 else None

    def create(self, repo_root: str, base_commit: str, scratch_root: str) -> Path:
        target = Path(scratch_root) / f"score-{uuid.uuid4().hex}"
        target.parent.mkdir(parents=True, exist_ok=True)
        done = _git(repo_root, "worktree", "add", "--detach", str(target), base_commit)
        if done.returncode != 0:
            raise WorkspaceError(done.stderr.decode(errors="replace")[-400:])
        return target

    def apply_diff(self, worktree: Path, diff: str) -> str | None:
        checked = _git(worktree, "apply", "--check", "-", stdin=diff)
        if checked.returncode != 0:
            return checked.stderr.decode(errors="replace")[-400:]
        applied = _git(worktree, "apply", "-", stdin=diff)
        if applied.returncode != 0:
            return applied.stderr.decode(errors="replace")[-400:]
        return None

    def place_file(self, worktree: Path, path: str, data: bytes) -> None:
        target = (worktree / path).resolve()
        if not target.is_relative_to(worktree.resolve()):
            raise WorkspaceError(f"check file path escapes the worktree: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def remove(self, repo_root: str, worktree: Path) -> None:
        _git(repo_root, "worktree", "remove", "--force", str(worktree))


class ShellRunner:
    """ProtocolCodeScoreRunner over bash, one command under a wall-clock limit."""

    def run(
        self, command: str, cwd: Path, timeout_s: float, tail_chars: int
    ) -> RunOutcome:
        started = time.monotonic()
        try:
            done = subprocess.run(
                ["bash", "-c", command],
                cwd=cwd,
                env=scrub_database_environment(os.environ),
                capture_output=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as expired:
            tail = ((expired.stdout or b"") + (expired.stderr or b"")).decode(
                errors="replace"
            )
            return RunOutcome(
                None,
                True,
                time.monotonic() - started,
                tail[-tail_chars:] if tail_chars else "",
            )
        text = (done.stdout + done.stderr).decode(errors="replace")
        return RunOutcome(
            done.returncode,
            False,
            time.monotonic() - started,
            text[-tail_chars:] if tail_chars else "",
        )


__all__ = ["GitWorkspace", "ShellRunner"]
