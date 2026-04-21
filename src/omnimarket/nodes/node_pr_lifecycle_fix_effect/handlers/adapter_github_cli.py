# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live GitHub CLI adapter for pr_lifecycle_fix_effect.

Implements ``ProtocolGitHubAdapter`` against the local ``gh`` CLI so the
orchestrator's Track B (FIXING phase) causes real external state changes
instead of the ``_NoopGitHubAdapter`` default. Related: OMN-9284.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

logger = logging.getLogger(__name__)


class GitHubCliAdapter:
    """Shell out to ``gh`` to rerun failed checks and resolve BEHIND branches.

    * ``rerun_failed_checks`` calls ``gh pr view`` to enumerate failed check
      runs for the PR and invokes ``gh run rerun --failed`` for each unique
      run id. Re-runs are per-run because ``gh pr checks`` does not expose a
      PR-level ``--rerun`` flag.
    * ``resolve_conflicts`` calls ``gh pr update-branch`` which resolves the
      common case (PR merely behind base). Structural conflicts that
      ``update-branch`` cannot resolve fall through and are surfaced to a
      human-agent dispatch path.
    """

    async def rerun_failed_checks(self, repo: str, pr_number: int) -> str:
        run_ids = await self._failed_run_ids(repo, pr_number)
        if not run_ids:
            return f"no failed checks on {repo}#{pr_number}"
        for run_id in run_ids:
            await self._run(
                ["gh", "run", "rerun", run_id, "--failed", "--repo", repo],
                context=f"rerun {repo} run_id={run_id}",
            )
        return f"rerequested {len(run_ids)} failed run(s) on {repo}#{pr_number}"

    async def resolve_conflicts(self, repo: str, pr_number: int) -> str:
        rc, stdout, stderr = await self._run(
            [
                "gh",
                "pr",
                "update-branch",
                str(pr_number),
                "--repo",
                repo,
            ],
            context=f"update-branch {repo}#{pr_number}",
            check=False,
        )
        if rc == 0:
            return f"update-branch succeeded on {repo}#{pr_number}"
        detail = (stderr or stdout or "unknown error").strip().splitlines()[0]
        msg = (
            f"update-branch failed on {repo}#{pr_number} (exit {rc}): {detail} "
            "— falling back to manual resolution"
        )
        raise RuntimeError(msg)

    async def _failed_run_ids(self, repo: str, pr_number: int) -> list[str]:
        rc, stdout, _ = await self._run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "statusCheckRollup",
            ],
            context=f"pr view {repo}#{pr_number}",
        )
        if rc != 0 or not stdout.strip():
            return []
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            logger.warning(
                "pr view returned non-JSON for %s#%s: %s", repo, pr_number, exc
            )
            return []
        checks = payload.get("statusCheckRollup") or []
        ids: list[str] = []
        seen: set[str] = set()
        for check in checks:
            conclusion = (check.get("conclusion") or "").upper()
            if conclusion not in {
                "FAILURE",
                "TIMED_OUT",
                "CANCELLED",
                "ACTION_REQUIRED",
            }:
                continue
            details = check.get("detailsUrl") or ""
            run_id = _run_id_from_details_url(details)
            if run_id and run_id not in seen:
                seen.add(run_id)
                ids.append(run_id)
        return ids

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
            # ProcessLookupError is possible if the child exited between the
            # timeout firing and the kill call — suppress it rather than
            # masking the original timeout.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            # Best-effort drain with its own bounded timeout; we already know
            # the child is dead and only need to reap the zombie.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.communicate(), timeout=1.0)
            raise RuntimeError(f"{context} timed out after {timeout_s:.0f}s") from exc
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        rc = proc.returncode if proc.returncode is not None else -1
        if check and rc != 0:
            detail = (stderr or stdout or "no output").strip().splitlines()[:1]
            msg = f"{context} failed (exit {rc}): {detail[0] if detail else ''}"
            raise RuntimeError(msg)
        return rc, stdout, stderr


def _run_id_from_details_url(details_url: str) -> str | None:
    """Parse a GitHub check ``detailsUrl`` of the form
    ``https://github.com/<owner>/<repo>/actions/runs/<run_id>/...`` → ``<run_id>``.
    """
    if not details_url or "/actions/runs/" not in details_url:
        return None
    tail = details_url.split("/actions/runs/", 1)[1]
    run_id = tail.split("/", 1)[0].split("?", 1)[0]
    return run_id or None


__all__ = ["GitHubCliAdapter"]
