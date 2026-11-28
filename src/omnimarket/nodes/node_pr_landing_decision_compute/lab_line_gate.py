# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Lab-line gate's pure reading: a head merges only when the newest Lab line names that exact head (OMN-26736).

Goal rule (2026-11-26): nothing merges until proven at that exact head on the lab. A Lab line naming an older head,
or no Lab line, reads ``needs_proof``. A PR with no Lab-line fact reads ``needs_proof`` only when the policy expects
the fact (``lab_line_read_expected``) and is decided as before otherwise. No clock and no I/O.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingPolicy,
    ModelLandingPrFacts,
)


class EnumLabLineVerdict(StrEnum):
    PASS = "pass"
    NEEDS_PROOF = "needs_proof"


class ModelLabLineReading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumLabLineVerdict
    detail: str


def read_lab_line_gate(
    p: ModelLandingPrFacts, policy: ModelLandingPolicy
) -> ModelLabLineReading | None:
    """The reading of the PR's newest Lab line against its head, or None when the gate holds nothing."""
    fact = p.lab_line
    if fact is None:
        if policy.lab_line_read_expected:
            return ModelLabLineReading(
                verdict=EnumLabLineVerdict.NEEDS_PROOF,
                detail="no Lab line was read for the PR",
            )
        return None
    if fact.named_head is None:
        return ModelLabLineReading(
            verdict=EnumLabLineVerdict.NEEDS_PROOF, detail="the PR carries no Lab line"
        )
    if fact.named_head != p.head_sha:
        return ModelLabLineReading(
            verdict=EnumLabLineVerdict.NEEDS_PROOF,
            detail=f"the newest Lab line names {fact.named_head}, not the head {p.head_sha}",
        )
    return ModelLabLineReading(
        verdict=EnumLabLineVerdict.PASS, detail="the newest Lab line names the head"
    )


__all__ = ["EnumLabLineVerdict", "ModelLabLineReading", "read_lab_line_gate"]
