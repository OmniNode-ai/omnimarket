# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Facts and triggers in, next state out of node_contract_seams_reducer (OMN-21627)."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelContractSeamsFacts(BaseModel):
    """The counts the guards read; each step of the orchestrator computes its own."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    design_issue_count: int = Field(default=0, ge=0)
    contract_count: int = Field(default=0, ge=0)
    unscaffolded_count: int = Field(default=0, ge=0)
    seam_count: int = Field(default=0, ge=0)
    seam_mismatch_count: int = Field(default=0, ge=0)
    seam_unmatched_count: int = Field(default=0, ge=0)
    walk_issue_count: int = Field(default=0, ge=0)
    chain_count: int = Field(default=0, ge=0)
    undriven_path_count: int = Field(default=0, ge=0)
    sweep_passed: bool = False
    tail_delivered: bool = False


class ModelContractSeamsTransitionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: str | None = None
    trigger: str = Field(min_length=1)
    facts: ModelContractSeamsFacts = ModelContractSeamsFacts()
    now: AwareDatetime


class ModelContractSeamsTransitionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prior_state: str
    state: str
    moved: bool
    error: str | None = None
    failed_conditions: tuple[str, ...] = ()
    decided_at: AwareDatetime


__all__ = [
    "ModelContractSeamsFacts",
    "ModelContractSeamsTransitionRequest",
    "ModelContractSeamsTransitionResult",
]
