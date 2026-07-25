# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed request payload for the admin-merge fallback handler."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.pr_arm_gate import ModelArmGatePolicy
from omnimarket.nodes.node_pr_lifecycle_inventory_compute.models.model_pr_lifecycle_inventory import (
    ModelStuckQueueEntry,
)


class ModelAdminMergeRequest(BaseModel):
    """Request to run an admin-merge fallback pass over stuck PRs.

    OMN-15064: a raw admin merge (``PUT /pulls/{n}/merge``) is exactly the
    kind of merge-queue mutation the OMN-14151 arm-gate choke point exists to
    govern. ``policy`` is the SAME ``ModelArmGatePolicy`` shape every other
    arm/merge surface in this repo is gated by (``HandlerPrArmGate``, the
    orchestrator's ``_call_merge_fanout`` and ``_remediate_stalled_queue_prs``)
    — not a second, separately-bypassable switch.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stuck_prs: list[ModelStuckQueueEntry] = Field(
        default_factory=list,
        description="PRs identified as stuck by inventory compute.",
    )
    enable_admin_merge_fallback: bool = Field(
        default=False,
        description=(
            "Opt-in master switch for the admin-merge fallback capability. "
            "Default OFF (OMN-15064) — a destructive raw-merge capability "
            "must not default on. Set True to opt in; even when opted in, "
            "``policy`` must independently satisfy action_mode=ENFORCE and "
            "kill_switch=False before any merge is attempted."
        ),
    )
    policy: ModelArmGatePolicy = Field(
        default_factory=ModelArmGatePolicy,
        description=(
            "OMN-14151/OMN-15064 choke point. Defaults to the SAFE "
            "(zero-mutation) policy (action_mode=report_only, "
            "kill_switch=True). An operator must explicitly select "
            "action_mode=enforce AND disengage the kill switch, in addition "
            "to enable_admin_merge_fallback=True, before any stuck PR is "
            "admin-merged."
        ),
    )
    dry_run: bool = Field(
        default=False, description="When True, log intent without merging."
    )


__all__ = ["ModelAdminMergeRequest"]
