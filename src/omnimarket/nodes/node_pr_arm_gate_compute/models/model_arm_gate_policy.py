# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EnumArmActionMode / ModelArmGatePolicy — the merge-queue governor's action mode.

OMN-14151 (corrected arm-gate design). ``action_mode`` and ``kill_switch`` are
folded INTO the gate's ARM/WITHHOLD decision rather than checked separately by
the orchestrator — a single choke point a CI guard can protect, instead of a
second check that could be bypassed by a code path that forgets to call it.

Both fields default to the SAFE (report-only / killed) value: an operator must
explicitly opt into ENFORCE and explicitly disengage the kill switch before any
real queue mutation is possible. This is a deliberate flip from the pre-OMN-14151
posture, where ``dry_run=False`` alone was enough to mutate the queue.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumArmActionMode(StrEnum):
    """The merge-queue governor's action mode.

    REPORT_ONLY: classify and log ARM/WITHHOLD decisions; never mutate GitHub.
    ENFORCE: PRs that clear every ARM criterion are actually merged, subject to
        ``wave_cap`` and the kill switch.
    """

    REPORT_ONLY = "report_only"
    ENFORCE = "enforce"


class ModelArmGatePolicy(BaseModel):
    """Operator-controlled policy consumed by the arm-gate decision.

    Fail-closed defaults: REPORT_ONLY action mode and an engaged kill switch,
    so an un-configured caller can never accidentally arm a merge.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_mode: EnumArmActionMode = Field(
        default=EnumArmActionMode.REPORT_ONLY,
        description="report_only (default, zero mutation) or enforce (opt-in).",
    )
    kill_switch: bool = Field(
        default=True,
        description=(
            "Emergency stop. Defaults ENGAGED (True) — an operator must "
            "explicitly disengage it (set False) in addition to selecting "
            "action_mode=enforce before any PR can arm."
        ),
    )
    wave_cap: int = Field(
        default=3,
        ge=0,
        description=(
            "Maximum number of PRs armed in a single enforce pass, bounding "
            "the blast radius of one sweep. 0 arms nothing regardless of "
            "action_mode."
        ),
    )
    enable_stall_remediation: bool = Field(
        default=False,
        description=(
            "Opt-in flag for merge-queue stall remediation (dequeue + "
            "re-enqueue an already-armed PR to re-mint a stuck merge-group "
            "SHA). This is a SEPARATE operation from readiness-arming an "
            "unarmed PR — it re-mints an already-armed PR's queue entry, so it "
            "is gated by this flag rather than by the arm-gate's per-PR ARM "
            "decision. Still requires action_mode=enforce and a disengaged "
            "kill_switch."
        ),
    )


__all__: list[str] = ["EnumArmActionMode", "ModelArmGatePolicy"]
