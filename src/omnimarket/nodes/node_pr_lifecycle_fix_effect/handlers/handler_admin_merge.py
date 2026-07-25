# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerAdminMerge — admin merge fallback for stuck merge queue PRs.

Consumes ModelStuckQueueEntry list from InventoryResult.stuck_queue_prs.

OMN-15064: gated by the SAME OMN-14151 choke point as every other arm/merge
surface (``ModelArmGatePolicy``: ``action_mode`` + ``kill_switch``), in
addition to the ``enable_admin_merge_fallback`` opt-in (default OFF). All
three must be satisfied — ``enable_admin_merge_fallback=True`` AND
``action_mode=enforce`` AND ``kill_switch=False`` — before any PR is
admin-merged. An opted-in caller (``enable_admin_merge_fallback=True``) whose
policy leaves the gate closed on a real (non-dry-run) pass gets a loud
``AdminMergeGateClosedError``, not a quiet skip — a silent skip is
indistinguishable from the code path never being reached.

Emits explicit log line "ADMIN MERGE TRIGGERED pr={pr_number} repo={repo}"
before acting.

Related:
    - OMN-8207: Task 10 — Add HandlerCommentResolution + HandlerAdminMerge
    - OMN-8206: Task 9 — Stuck merge queue detection (produces stuck_queue_prs)
    - OMN-14151: merge-queue governor arm-gate choke point
    - OMN-15064: bring HandlerAdminMerge under the OMN-14151 choke point
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.events.pr_arm_gate import EnumArmActionMode
from omnimarket.github_api import rest_json, split_repo
from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_admin_merge_request import (
    ModelAdminMergeRequest,
)

logger = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


class AdminMergeGateClosedError(RuntimeError):
    """Raised when an opted-in admin-merge pass is attempted with the
    OMN-14151/OMN-15064 arm-gate choke point closed.

    ``enable_admin_merge_fallback=True`` is an explicit statement of intent
    to admin-merge stuck PRs. Silently downgrading that intent to a no-op
    because ``policy`` also failed to open is indistinguishable from this
    handler never being invoked at all — see OMN-15064.
    """


def _resolve_github_token() -> str:
    """Resolve the GitHub token from the contract-declared ref (OMN-12856).

    ``env_var_fallback`` (OMN-14452): the deployed lane's secret resolver is
    LLM/Slack-scoped with convention fallback disabled and never resolves
    ``GITHUB_TOKEN`` — falling back to the literal env var (already passed
    straight through as a container env var) resolves it instead of raising.
    """
    ref = contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN")
    secret = resolve_api_key(ref, env_var_fallback=ref)
    if secret is None:
        raise RuntimeError(
            f"api_key_ref {ref!r} resolved to None — "
            "ensure GITHUB_TOKEN is set in the secret store."
        )
    return secret.get_secret_value()


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class ModelAdminMergeResult(BaseModel):
    """Result of an admin merge fallback pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prs_merged: int = 0
    prs_skipped: int = 0
    prs_failed: int = 0
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Adapter protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ProtocolAdminMergeAdapter(Protocol):
    """Minimal GitHub merge operations for admin merge fallback."""

    async def admin_merge(self, repo: str, pr_number: int) -> None:
        """Merge a PR immediately via GitHub's pull-request merge API."""
        ...


# ---------------------------------------------------------------------------
# Default live adapter
# ---------------------------------------------------------------------------


class _LiveAdminMergeAdapter:
    async def admin_merge(self, repo: str, pr_number: int) -> None:
        await asyncio.to_thread(self._admin_merge_sync, repo, pr_number)

    def _admin_merge_sync(self, repo: str, pr_number: int) -> None:
        token = _resolve_github_token()
        owner, repo_name = split_repo(repo)
        rest_json(
            "PUT",
            f"/repos/{owner}/{repo_name}/pulls/{pr_number}/merge",
            token=token,
            body={"merge_method": "squash"},
        )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerAdminMerge:
    """Admin merge fallback for PRs stuck in merge queue >30min.

    Fires only when ALL of the following hold (OMN-14151/OMN-15064 choke
    point, default OFF/SAFE):
      - ``enable_admin_merge_fallback=True`` (opt-in master switch)
      - ``policy.action_mode == EnumArmActionMode.ENFORCE``
      - ``policy.kill_switch is False``

    Logs an explicit "ADMIN MERGE TRIGGERED" line before each merge action
    for audit trails. An opted-in but gate-closed real (non-dry-run) pass
    raises ``AdminMergeGateClosedError`` instead of silently no-op'ing.
    """

    def __init__(self, adapter: ProtocolAdminMergeAdapter | None = None) -> None:
        self._adapter: ProtocolAdminMergeAdapter = adapter or _LiveAdminMergeAdapter()

    @property
    def handler_type(self) -> str:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> str:
        return "EFFECT"

    @property
    def correlation_id(self) -> UUID | None:
        return None

    async def handle(self, payload: ModelAdminMergeRequest) -> ModelAdminMergeResult:
        """Admin-merge stuck PRs iff the opt-in AND the arm-gate policy agree.

        Args:
            payload: Typed request — stuck PRs, opt-in flag, arm-gate policy,
                dry-run flag.

        Returns:
            ModelAdminMergeResult with merge counts.

        Raises:
            AdminMergeGateClosedError: opt-in is True, there are stuck PRs,
                this is a real (non-dry-run) pass, but ``payload.policy``
                does not satisfy action_mode=ENFORCE + kill_switch=False.
        """
        dry_run = payload.dry_run

        if not payload.enable_admin_merge_fallback:
            logger.info(
                "admin-merge: skipped (enable_admin_merge_fallback=False), "
                "stuck_prs=%d",
                len(payload.stuck_prs),
            )
            return ModelAdminMergeResult(
                prs_skipped=len(payload.stuck_prs), dry_run=dry_run
            )

        gate_open = (
            payload.policy.action_mode is EnumArmActionMode.ENFORCE
            and not payload.policy.kill_switch
        )
        if not gate_open:
            if payload.stuck_prs and not dry_run:
                raise AdminMergeGateClosedError(
                    "admin-merge requested (enable_admin_merge_fallback=True) "
                    f"for {len(payload.stuck_prs)} stuck PR(s) but the "
                    "OMN-14151/OMN-15064 arm-gate choke point is closed: "
                    f"action_mode={payload.policy.action_mode.value!r} "
                    f"kill_switch={payload.policy.kill_switch!r}. Admin merge "
                    "requires policy.action_mode=enforce AND "
                    "policy.kill_switch=False in addition to "
                    "enable_admin_merge_fallback=True. See OMN-15064."
                )
            logger.info(
                "admin-merge: withheld (arm-gate closed) "
                "action_mode=%s kill_switch=%s dry_run=%s stuck_prs=%d",
                payload.policy.action_mode.value,
                payload.policy.kill_switch,
                dry_run,
                len(payload.stuck_prs),
            )
            return ModelAdminMergeResult(
                prs_skipped=len(payload.stuck_prs), dry_run=dry_run
            )

        prs_merged = 0
        prs_skipped = 0
        prs_failed = 0

        for pr in payload.stuck_prs:
            logger.warning(
                "ADMIN MERGE TRIGGERED pr=%s repo=%s queue_age_minutes=%.1f dry_run=%s",
                pr.pr_number,
                pr.repo,
                pr.queue_age_minutes,
                dry_run,
            )
            if dry_run:
                prs_merged += 1
                continue
            try:
                await self._adapter.admin_merge(repo=pr.repo, pr_number=pr.pr_number)
                prs_merged += 1
                logger.info(
                    "admin-merge succeeded: pr=%s repo=%s", pr.pr_number, pr.repo
                )
            except Exception as exc:
                prs_failed += 1
                logger.warning(
                    "admin-merge failed: pr=%s repo=%s error=%s",
                    pr.pr_number,
                    pr.repo,
                    exc,
                    exc_info=True,
                )

        return ModelAdminMergeResult(
            prs_merged=prs_merged,
            prs_skipped=prs_skipped,
            prs_failed=prs_failed,
            dry_run=dry_run,
        )


__all__: list[str] = [
    "AdminMergeGateClosedError",
    "HandlerAdminMerge",
    "ModelAdminMergeResult",
    "ProtocolAdminMergeAdapter",
]
