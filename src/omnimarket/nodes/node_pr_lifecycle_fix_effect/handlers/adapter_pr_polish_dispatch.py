# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live agent-dispatch adapter for pr_lifecycle_fix_effect.

Spawns detached ``claude -p`` sub-agents for PR review-fix and CodeRabbit
auto-reply flows. Before this adapter existed, the orchestrator wired
``_NoopAgentDispatchAdapter`` which returned descriptive strings without
spawning any subprocess — BLOCKED PRs silently accumulated. Related:
OMN-9284, mirrors the OMN-9276 pattern.
"""

from __future__ import annotations

import logging
import os
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class ProtocolSubprocessSpawner(Protocol):
    """Seam that lets tests assert on Popen args without actually spawning."""

    def __call__(
        self,
        argv: list[str],
        *,
        stdout: int,
        stderr: int,
        start_new_session: bool,
        env: dict[str, str] | None,
    ) -> object: ...


def _default_spawner(
    argv: list[str],
    *,
    stdout: int,
    stderr: int,
    start_new_session: bool,
    env: dict[str, str] | None,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        argv,
        stdout=stdout,
        stderr=stderr,
        start_new_session=start_new_session,
        env=env,
    )


class PrPolishDispatchAdapter:
    """Dispatch ``/onex:pr_polish`` and ``/onex:coderabbit_triage`` sub-agents.

    Each dispatch:
      * Creates ``$ONEX_STATE_DIR/pr-polish/{repo_slug}-{pr}-{run_id}/`` and
        writes a ``dispatch.json`` breadcrumb so subsequent ticks can see that
        a worker was actually spawned (the signal that was missing pre-9284).
      * Spawns ``claude -p '<skill invocation>'`` detached
        (``start_new_session=True``, stdout/stderr redirected to a log file
        inside the state dir). The orchestrator does not block on the worker.
    """

    def __init__(
        self,
        *,
        claude_bin: str | None = None,
        state_dir: Path | None = None,
        spawner: ProtocolSubprocessSpawner | None = None,
    ) -> None:
        self._claude_bin = claude_bin or os.environ.get("CLAUDE_BIN", "claude")
        self._state_dir = state_dir or self._resolve_state_dir()
        self._spawner: ProtocolSubprocessSpawner = spawner or _default_spawner

    @staticmethod
    def _resolve_state_dir() -> Path:
        return Path(os.environ.get("ONEX_STATE_DIR", str(Path.home() / ".onex_state")))

    async def dispatch_review_fix(
        self, repo: str, pr_number: int, ticket_id: str | None
    ) -> str:
        skill_cmd = f"/onex:pr_polish --repo {repo} --pr {pr_number}"
        if ticket_id:
            skill_cmd += f" --ticket {ticket_id}"
        return self._spawn("review-fix", repo, pr_number, skill_cmd, ticket_id)

    async def dispatch_coderabbit_reply(self, repo: str, pr_number: int) -> str:
        skill_cmd = f"/onex:coderabbit_triage --repo {repo} --pr {pr_number}"
        return self._spawn("coderabbit-reply", repo, pr_number, skill_cmd, None)

    def _spawn(
        self,
        kind: str,
        repo: str,
        pr_number: int,
        skill_cmd: str,
        ticket_id: str | None,
    ) -> str:
        run_id = uuid.uuid4().hex[:12]
        run_dir = self._make_run_dir(repo, pr_number, run_id)
        log_path = run_dir / "worker.log"
        argv = [self._claude_bin, "-p", skill_cmd]

        self._write_breadcrumb(run_dir, kind, repo, pr_number, ticket_id, argv, run_id)

        log_fh = log_path.open("ab")
        try:
            self._spawner(
                argv,
                stdout=log_fh.fileno(),
                stderr=log_fh.fileno(),
                start_new_session=True,
                env=None,
            )
        finally:
            log_fh.close()

        logger.info(
            "pr_polish_dispatch: kind=%s repo=%s pr=%s run_id=%s state_dir=%s",
            kind,
            repo,
            pr_number,
            run_id,
            run_dir,
        )
        return f"dispatched {kind} agent on {repo}#{pr_number} run_id={run_id}"

    def _make_run_dir(self, repo: str, pr_number: int, run_id: str) -> Path:
        slug = repo.replace("/", "-")
        run_dir = self._state_dir / "pr-polish" / f"{slug}-{pr_number}-{run_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    @staticmethod
    def _write_breadcrumb(
        run_dir: Path,
        kind: str,
        repo: str,
        pr_number: int,
        ticket_id: str | None,
        argv: list[str],
        run_id: str,
    ) -> None:
        import json

        payload = {
            "kind": kind,
            "repo": repo,
            "pr_number": pr_number,
            "ticket_id": ticket_id,
            "argv": argv,
            "run_id": run_id,
            "dispatched_at": datetime.now(tz=UTC).isoformat(),
        }
        (run_dir / "dispatch.json").write_text(json.dumps(payload, indent=2))


__all__ = ["PrPolishDispatchAdapter", "ProtocolSubprocessSpawner"]
