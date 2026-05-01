# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live GitHub merge-queue adapter for pr_lifecycle_merge_effect.

Track A green-PR handling must mutate GitHub state. A descriptive no-op is only
valid for dry-run/tests, not for the orchestrator's default production wiring.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_ENABLE_AUTO_MERGE_MUTATION = (
    "mutation($id: ID!, $method: PullRequestMergeMethod!) {"
    " enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: $method})"
    " { pullRequest { number } }"
    "}"
)

_ENQUEUE_MUTATION = (
    "mutation($id: ID!) { enqueuePullRequest(input: {pullRequestId: $id}) "
    "{ mergeQueueEntry { position } } }"
)

_DEQUEUE_MUTATION = (
    "mutation($id: ID!) { dequeuePullRequest(input: {id: $id}) "
    "{ pullRequest { number } } }"
)

_NO_MERGE_QUEUE_MARKERS = (
    "does not have a merge queue",
    "merge queue is not enabled",
    "merge_queue_not_enabled",
)

_MAX_QUEUE_STALL_ATTEMPTS_PER_HOUR = 2


class GitHubMergeQueueAdapter:
    """Execute PR auto-merge and merge-queue enqueue via ``gh``.

    The adapter uses GraphQL instead of `gh pr merge` because tonight's dogfood
    showed a CLEAN auto-merge-enabled PR can remain outside the merge queue
    until `enqueuePullRequest` is called explicitly.
    """

    def __init__(self, *, state_dir: Path | None = None) -> None:
        self._state_dir = state_dir

    async def merge_pr(
        self,
        repo: str,
        pr_number: int,
        use_merge_queue: bool,
    ) -> str:
        node_id = await self._fetch_pr_node_id(repo, pr_number)
        await self._enable_auto_merge(node_id, repo, pr_number)

        if not use_merge_queue:
            return f"auto-merge enabled for {repo}#{pr_number}"

        enqueued, position, skipped_no_queue = await self._enqueue_pr(node_id)
        if skipped_no_queue:
            return f"auto-merge enabled for {repo}#{pr_number}; repo has no merge queue"
        if not enqueued:
            raise RuntimeError(f"enqueuePullRequest did not enqueue {repo}#{pr_number}")
        return (
            f"auto-merge enabled and enqueued {repo}#{pr_number} at position {position}"
        )

    async def post_pr_comment(
        self,
        repo: str,
        pr_number: int,
        body: str,
    ) -> None:
        await self._run(
            ["gh", "pr", "comment", str(pr_number), "--repo", repo, "--body", body],
            context=f"comment {repo}#{pr_number}",
        )

    async def remediate_queue_stall(self, repo: str, pr_number: int) -> str:
        """Recover an AWAITING_CHECKS queue stall by dequeueing and requeueing."""
        attempts = self._recent_queue_stall_attempts(repo, pr_number)
        if len(attempts) >= _MAX_QUEUE_STALL_ATTEMPTS_PER_HOUR:
            return (
                f"queue stall remediation skipped for {repo}#{pr_number}: "
                "2 attempts in the last hour"
            )

        attempt_number = len(attempts) + 1
        self._record_queue_stall_attempt(repo, pr_number)
        node_id = await self._fetch_pr_node_id(repo, pr_number)
        try:
            await self._dequeue_pr(node_id, repo, pr_number)
            await self._enable_auto_merge(node_id, repo, pr_number)
            enqueued, position, skipped_no_queue = await self._enqueue_pr(node_id)
            if skipped_no_queue:
                raise RuntimeError("repo has no merge queue")
            if not enqueued:
                raise RuntimeError(
                    f"enqueuePullRequest did not requeue {repo}#{pr_number}"
                )
        except Exception:
            if attempt_number >= _MAX_QUEUE_STALL_ATTEMPTS_PER_HOUR:
                self._file_queue_stall_friction(repo, pr_number)
                logger.critical(
                    "PAGE_REQUIRED merge queue stall remediation failed twice: %s#%s",
                    repo,
                    pr_number,
                )
            raise
        return (
            f"queue stall remediated for {repo}#{pr_number}: "
            f"dequeued, auto-merge enabled, requeued at position {position}"
        )

    async def _fetch_pr_node_id(self, repo: str, pr_number: int) -> str:
        _rc, stdout, _stderr = await self._run(
            ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "id"],
            context=f"pr view {repo}#{pr_number}",
        )
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"pr view returned invalid JSON for {repo}#{pr_number}"
            ) from exc

        node_id = payload.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise RuntimeError(f"pr view returned empty node id for {repo}#{pr_number}")
        return node_id

    async def _enable_auto_merge(
        self,
        node_id: str,
        repo: str,
        pr_number: int,
    ) -> None:
        await self._run(
            [
                "gh",
                "api",
                "graphql",
                "-F",
                f"id={node_id}",
                "-F",
                "method=SQUASH",
                "-f",
                f"query={_ENABLE_AUTO_MERGE_MUTATION}",
            ],
            context=f"enable auto-merge {repo}#{pr_number}",
        )

    async def _dequeue_pr(self, node_id: str, repo: str, pr_number: int) -> None:
        await self._run(
            [
                "gh",
                "api",
                "graphql",
                "-F",
                f"id={node_id}",
                "-f",
                f"query={_DEQUEUE_MUTATION}",
            ],
            context=f"dequeue {repo}#{pr_number}",
        )

    async def _enqueue_pr(self, node_id: str) -> tuple[bool, int | None, bool]:
        rc, stdout, stderr = await self._run(
            [
                "gh",
                "api",
                "graphql",
                "-F",
                f"id={node_id}",
                "-f",
                f"query={_ENQUEUE_MUTATION}",
            ],
            context="enqueuePullRequest",
            check=False,
        )
        combined = f"{stderr} {stdout}".lower()
        if rc != 0:
            if any(marker in combined for marker in _NO_MERGE_QUEUE_MARKERS):
                return False, None, True
            detail = (stderr or stdout or "no output").strip().splitlines()[:1]
            raise RuntimeError(
                f"enqueuePullRequest failed: {detail[0] if detail else 'unknown error'}"
            )

        try:
            payload = json.loads(stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("enqueuePullRequest returned invalid JSON") from exc

        entry = (
            payload.get("data", {}).get("enqueuePullRequest", {}).get("mergeQueueEntry")
        )
        if not isinstance(entry, dict):
            raise RuntimeError("enqueuePullRequest returned no mergeQueueEntry")
        position = entry.get("position")
        if not isinstance(position, int):
            raise RuntimeError("enqueuePullRequest returned no integer queue position")
        return True, position, False

    async def _run(
        self,
        argv: list[str],
        *,
        context: str,
        check: bool = True,
        timeout_s: float = 30.0,
    ) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise RuntimeError(f"{context} failed to start gh: {exc}") from exc
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )
        except TimeoutError as exc:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.communicate(), timeout=1.0)
            raise RuntimeError(f"{context} timed out after {timeout_s:.0f}s") from exc

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        rc = proc.returncode if proc.returncode is not None else -1
        if check and rc != 0:
            detail = (stderr or stdout or "no output").strip().splitlines()[:1]
            raise RuntimeError(
                f"{context} failed (exit {rc}): {detail[0] if detail else ''}"
            )
        return rc, stdout, stderr

    def _queue_stall_state_file(self) -> Path:
        base = self._state_dir
        if base is None:
            base = Path(os.environ.get("ONEX_STATE_DIR", ".onex_state"))
        return base / "merge-sweep" / "queue-stall-remediation.json"

    def _read_queue_stall_attempt_state(self) -> dict[str, list[str]]:
        state_file = self._queue_stall_state_file()
        try:
            payload = json.loads(state_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if not isinstance(payload, dict):
            return {}

        state: dict[str, list[str]] = {}
        for key, value in payload.items():
            if isinstance(key, str) and isinstance(value, list):
                state[key] = [v for v in value if isinstance(v, str)]
        return state

    def _write_queue_stall_attempt_state(self, state: dict[str, list[str]]) -> None:
        state_file = self._queue_stall_state_file()
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    def _recent_queue_stall_attempts(self, repo: str, pr_number: int) -> list[str]:
        cutoff = datetime.now(tz=UTC) - timedelta(hours=1)
        attempts = self._read_queue_stall_attempt_state().get(
            self._queue_stall_key(repo, pr_number), []
        )
        recent: list[str] = []
        for raw in attempts:
            try:
                attempted_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if attempted_at > cutoff:
                recent.append(raw)
        return recent

    def _record_queue_stall_attempt(self, repo: str, pr_number: int) -> None:
        key = self._queue_stall_key(repo, pr_number)
        state = self._read_queue_stall_attempt_state()
        state[key] = [
            *self._recent_queue_stall_attempts(repo, pr_number),
            datetime.now(tz=UTC).isoformat(),
        ]
        self._write_queue_stall_attempt_state(state)

    @staticmethod
    def _queue_stall_key(repo: str, pr_number: int) -> str:
        return f"{repo}#{pr_number}"

    def _file_queue_stall_friction(self, repo: str, pr_number: int) -> None:
        base = self._state_dir
        if base is None:
            base = Path(os.environ.get("ONEX_STATE_DIR", ".onex_state"))
        friction_dir = base / "friction"
        friction_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{repo}-{pr_number}").strip("-")
        now = datetime.now(tz=UTC)
        friction_file = (
            friction_dir
            / f"{now.date().isoformat()}-merge-queue-stall-remediation-{slug}.md"
        )
        friction_file.write_text(
            "\n".join(
                [
                    f"# Merge Queue Stall Remediation: {repo}#{pr_number}",
                    "",
                    f"- repo: `{repo}`",
                    f"- pr_number: `{pr_number}`",
                    f"- recorded_at: `{now.isoformat()}`",
                    "- friction_type: `merge_queue_check_suite_dispatch_stall`",
                    "- escalation: `page_required`",
                    "",
                    "Two dequeue/requeue remediation attempts failed within one hour.",
                    "",
                ]
            )
        )


__all__ = ["GitHubMergeQueueAdapter"]
