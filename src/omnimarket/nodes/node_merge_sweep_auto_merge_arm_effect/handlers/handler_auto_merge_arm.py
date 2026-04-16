# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_merge_sweep_auto_merge_arm_effect [OMN-8960].

EFFECT node. Serial-in-handler execution per Phase 1 audit.
Fires GitHub GraphQL enablePullRequestAutoMerge (SQUASH) inline.
Returns ModelHandlerOutput.for_effect(events=(completion,)).

NEVER calls gh pr merge --auto. NEVER uses --admin. Always GraphQL.
Idempotent: re-arming an already-armed PR returns success.
"""

from __future__ import annotations

import asyncio
import logging
import time
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_merge_sweep_auto_merge_arm_effect.models.model_auto_merge_armed_event import (
    ModelAutoMergeArmedEvent,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelAutoMergeArmCommand,
)

_log = logging.getLogger(__name__)

_GRAPHQL_MUTATION = (
    "mutation($id: ID!, $method: PullRequestMergeMethod!) {"
    "  enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: $method}) {"
    "    pullRequest { number }"
    "  }"
    "}"
)


class HandlerAutoMergeArmEffect:
    """EFFECT: arm auto-merge via GraphQL SQUASH, inline, serial."""

    async def handle(self, request: ModelAutoMergeArmCommand) -> ModelHandlerOutput:  # type: ignore[type-arg]
        """Arm auto-merge. Real work runs inline before returning."""
        t0 = time.monotonic()
        armed, error = await self._arm(request.pr_node_id, request.repo)
        elapsed = time.monotonic() - t0

        if armed:
            _log.info(
                "auto-merge armed: %s#%s (elapsed=%.2fs)",
                request.repo,
                request.pr_number,
                elapsed,
            )
        else:
            _log.error(
                "auto-merge arm failed: %s#%s error=%r (elapsed=%.2fs)",
                request.repo,
                request.pr_number,
                error,
                elapsed,
            )

        completion = ModelAutoMergeArmedEvent(
            pr_number=request.pr_number,
            repo=request.repo,
            correlation_id=request.correlation_id,
            run_id=request.run_id,
            total_prs=request.total_prs,
            armed=armed,
            error=error,
            elapsed_seconds=elapsed,
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id="node_merge_sweep_auto_merge_arm_effect",
            events=(completion,),
        )

    async def _arm(self, pr_node_id: str, repo: str) -> tuple[bool, str | None]:
        """Enable auto-merge via GraphQL. Idempotent per GitHub API contract."""
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "api",
            "graphql",
            "-F",
            f"id={pr_node_id}",
            "-F",
            "method=SQUASH",
            "-f",
            f"query={_GRAPHQL_MUTATION}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            return True, None
        return False, stderr.decode(errors="replace")
