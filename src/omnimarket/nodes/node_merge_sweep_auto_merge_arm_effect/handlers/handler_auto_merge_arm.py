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
import json
import logging
import os
import time
import urllib.error
import urllib.request
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
_GITHUB_GRAPHQL = "https://api.github.com/graphql"
_GITHUB_API_VERSION = "2026-03-10"
_REQUEST_TIMEOUT = 30.0


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
        return await asyncio.to_thread(self._arm_sync, pr_node_id, repo)

    def _arm_sync(self, pr_node_id: str, repo: str) -> tuple[bool, str | None]:
        token = os.environ.get("GH_PAT", "")
        if not token:
            return False, "GH_PAT environment variable is not set"

        payload = json.dumps(
            {
                "query": _GRAPHQL_MUTATION,
                "variables": {"id": pr_node_id, "method": "SQUASH"},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            _GITHUB_GRAPHQL,
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            return False, detail or str(exc)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            return False, str(exc)

        if body.get("errors"):
            return False, json.dumps(body["errors"])
        return True, None
