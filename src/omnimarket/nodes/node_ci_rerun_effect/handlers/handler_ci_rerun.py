# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_ci_rerun_effect [OMN-8962].

EFFECT node. Serial-in-handler execution per Phase 1 audit.
Triggers GitHub's rerun-failed-jobs API for the PR's most recent failed workflow
run. Only reruns failed jobs; does not retrigger successful ones.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_ci_rerun_effect.models.model_ci_rerun_triggered_event import (
    ModelCiRerunTriggeredEvent,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelCiRerunCommand,
)

_log = logging.getLogger(__name__)
_GITHUB_API_VERSION = "2026-03-10"
_REQUEST_TIMEOUT = 30.0


class HandlerCiRerunEffect:
    """EFFECT: trigger the GitHub rerun-failed-jobs API on a PR's failing run."""

    async def handle(self, request: ModelCiRerunCommand) -> ModelHandlerOutput:  # type: ignore[type-arg]
        """Trigger CI rerun. Real work runs inline before returning."""
        t0 = time.monotonic()
        triggered, error = await self._rerun(request.run_id_github, request.repo)
        elapsed = time.monotonic() - t0

        if triggered:
            _log.info(
                "CI rerun triggered: %s#%s run=%s (elapsed=%.2fs)",
                request.repo,
                request.pr_number,
                request.run_id_github,
                elapsed,
            )
        else:
            _log.error(
                "CI rerun failed: %s#%s run=%s error=%r (elapsed=%.2fs)",
                request.repo,
                request.pr_number,
                request.run_id_github,
                error,
                elapsed,
            )

        completion = ModelCiRerunTriggeredEvent(
            pr_number=request.pr_number,
            repo=request.repo,
            correlation_id=request.correlation_id,
            run_id=request.run_id,
            total_prs=request.total_prs,
            run_id_github=request.run_id_github,
            rerun_triggered=triggered,
            error=error,
            elapsed_seconds=elapsed,
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id="node_ci_rerun_effect",
            events=(completion,),
        )

    async def _rerun(self, run_id_github: str, repo: str) -> tuple[bool, str | None]:
        """Trigger GitHub's rerun-failed-jobs workflow-run API."""
        return await asyncio.to_thread(self._rerun_sync, run_id_github, repo)

    def _rerun_sync(self, run_id_github: str, repo: str) -> tuple[bool, str | None]:
        token = os.environ.get("GH_PAT", "")
        if not token:
            return False, "GH_PAT environment variable is not set"
        owner, _, repo_name = repo.partition("/")
        if not owner or not repo_name:
            return False, f"invalid repo slug: {repo!r}"

        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo_name}/actions/runs/{run_id_github}/rerun-failed-jobs",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT):
                return True, None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            if detail:
                try:
                    body = json.loads(detail)
                    message = body.get("message")
                    if isinstance(message, str) and message:
                        return False, message
                except json.JSONDecodeError:
                    pass
            return False, detail or str(exc)
        except (urllib.error.URLError, OSError) as exc:
            return False, str(exc)
