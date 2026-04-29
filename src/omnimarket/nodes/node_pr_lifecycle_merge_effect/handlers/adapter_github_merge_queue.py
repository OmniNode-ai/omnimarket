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

_NO_MERGE_QUEUE_MARKERS = (
    "does not have a merge queue",
    "merge queue is not enabled",
    "merge_queue_not_enabled",
)


class GitHubMergeQueueAdapter:
    """Execute PR auto-merge and merge-queue enqueue via ``gh``.

    The adapter uses GraphQL instead of `gh pr merge` because tonight's dogfood
    showed a CLEAN auto-merge-enabled PR can remain outside the merge queue
    until `enqueuePullRequest` is called explicitly.
    """

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


__all__ = ["GitHubMergeQueueAdapter"]
