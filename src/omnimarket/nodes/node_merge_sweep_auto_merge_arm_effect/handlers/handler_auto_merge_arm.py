# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_merge_sweep_auto_merge_arm_effect [OMN-8960].

EFFECT node. Serial-in-handler execution per Phase 1 audit.
Fires GitHub GraphQL enablePullRequestAutoMerge (SQUASH) inline.
Returns ModelHandlerOutput.for_effect(events=(completion,)).

NEVER calls gh pr merge --auto. NEVER uses --admin. Always GraphQL.
Idempotent: re-arming an already-armed PR returns success.

The GitHub token is resolved at handle() time from the contract-declared
``api_key_ref`` (``GITHUB_TOKEN``) — no direct ``os.environ`` read.

OMN-14151: this is one of the three legacy arm surfaces superseded by the
merge-queue governor's single gated arm path (node_pr_arm_gate_compute +
node_pr_lifecycle_merge_effect). Hard-gated fail-closed — the GraphQL mutation
never fires unless ``_LEGACY_ARM_ENV_VAR`` is explicitly enabled.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.config.env_flags import env_flag
from omnimarket.config.service_endpoints import GITHUB_GRAPHQL_URL
from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_merge_sweep_auto_merge_arm_effect.models.model_auto_merge_armed_event import (
    ModelAutoMergeArmedEvent,
)
from omnimarket.nodes.node_merge_sweep_auto_merge_arm_effect.models.model_auto_merge_unarmed_clean_alert_event import (
    ModelAutoMergeUnarmedCleanAlertEvent,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelAutoMergeArmCommand,
)

_log = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

# OMN-14151: legacy arm surface hard-gate. Safe default False — the GraphQL
# mutation is a no-op unless an operator explicitly opts this surface back in.
_LEGACY_ARM_ENV_VAR = "OMNIMARKET_LEGACY_MERGE_ARM_ENABLED"

_GRAPHQL_MUTATION = (
    "mutation($id: ID!, $method: PullRequestMergeMethod!) {"
    "  enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: $method}) {"
    "    pullRequest { number }"
    "  }"
    "}"
)
_GITHUB_GRAPHQL = GITHUB_GRAPHQL_URL
_GITHUB_API_VERSION = "2026-03-10"
_REQUEST_TIMEOUT = 30.0


class HandlerAutoMergeArmEffect:
    """EFFECT: arm auto-merge via GraphQL SQUASH, inline, serial."""

    async def handle(self, request: ModelAutoMergeArmCommand) -> ModelHandlerOutput:  # type: ignore[type-arg]
        """Arm auto-merge. Real work runs inline before returning.

        The GitHub token ref-name is sourced from the contract ``secrets`` block
        (OMN-12856) and resolved at the effect boundary via the canonical
        secret-store resolver — never read from env directly in this handler.
        """
        if not env_flag(_LEGACY_ARM_ENV_VAR, safe_default=False):
            _log.info(
                "auto-merge arm gated (no-op): %s#%s — legacy arm surface disabled "
                "by default (OMN-14151); set %s=true to re-enable",
                request.repo,
                request.pr_number,
                _LEGACY_ARM_ENV_VAR,
            )
            completion = ModelAutoMergeArmedEvent(
                pr_number=request.pr_number,
                repo=request.repo,
                correlation_id=request.correlation_id,
                run_id=request.run_id,
                total_prs=request.total_prs,
                armed=False,
                error=f"gated: legacy arm surface disabled ({_LEGACY_ARM_ENV_VAR} not enabled)",
                elapsed_seconds=0.0,
            )
            return ModelHandlerOutput.for_effect(
                input_envelope_id=uuid4(),
                correlation_id=request.correlation_id,
                handler_id="node_merge_sweep_auto_merge_arm_effect",
                events=(completion,),
            )

        # Resolve token ref-name from contract, then value from secret store.
        _github_ref = contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN")
        github_secret = await resolve_api_key_async(_github_ref)
        if github_secret is None:
            raise RuntimeError(
                f"api_key_ref {_github_ref!r} resolved to None — "
                "ensure GITHUB_TOKEN is set in the secret store."
            )
        token = github_secret.get_secret_value()

        t0 = time.monotonic()
        armed, error = await self._arm(request.pr_node_id, request.repo, token)
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

        # The arm command is published by triage only for CLEAN PRs (Rule 2),
        # so an arm failure here is exactly the "CLEAN but auto-merge not armed"
        # case (omnibase_core#1280). Emit a dedicated alert event alongside the
        # completion so the failed-to-arm case is observable on its own topic —
        # the completion event proves the effect ran; the alert names the failure
        # (OMN-13322). A missing OCC preflight (OMN-10485) surfaces here as the
        # GraphQL/arm error string.
        events: tuple[
            ModelAutoMergeArmedEvent | ModelAutoMergeUnarmedCleanAlertEvent, ...
        ]
        if armed:
            events = (completion,)
        else:
            alert = ModelAutoMergeUnarmedCleanAlertEvent(
                pr_number=request.pr_number,
                repo=request.repo,
                correlation_id=request.correlation_id,
                run_id=request.run_id,
                total_prs=request.total_prs,
                reason=error or "auto-merge arm failed with no error detail",
                elapsed_seconds=elapsed,
            )
            events = (completion, alert)

        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id="node_merge_sweep_auto_merge_arm_effect",
            events=events,
        )

    async def _arm(
        self, pr_node_id: str, repo: str, token: str
    ) -> tuple[bool, str | None]:
        """Enable auto-merge via GraphQL. Idempotent per GitHub API contract."""
        return await asyncio.to_thread(self._arm_sync, pr_node_id, repo, token)

    def _arm_sync(
        self, pr_node_id: str, repo: str, token: str
    ) -> tuple[bool, str | None]:
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
