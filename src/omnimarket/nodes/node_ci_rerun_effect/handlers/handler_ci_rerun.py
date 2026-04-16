# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_ci_rerun_effect [OMN-8962].

EFFECT node. Serial-in-handler execution per Phase 1 audit.
Triggers `gh run rerun --failed` for the PR's most recent failed workflow run.
Only reruns FAILED checks; does not retrigger successful ones.
Idempotent: repeated rerun calls on the same run ID are handled by gh.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_ci_rerun_effect.models.model_ci_rerun_triggered_event import (
    ModelCiRerunTriggeredEvent,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelCiRerunCommand,
)

_log = logging.getLogger(__name__)


class HandlerCiRerunEffect:
    """EFFECT: trigger gh run rerun --failed on a PR's failing run."""

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
        """Trigger gh run rerun --failed. Only reruns failed jobs."""
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "run",
            "rerun",
            run_id_github,
            "--failed",
            "--repo",
            repo,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return False, "gh run rerun timed out after 30s"
        except Exception as exc:
            return False, f"subprocess error: {exc}"
        if proc.returncode == 0:
            return True, None
        return False, stderr.decode(errors="replace")
