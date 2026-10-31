# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The run request, its config and its result for node_contract_seams_orchestrator (OMN-21627)."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.contract_seams_design import ModelContractSeamsDesign


class ModelContractSeamsConfig(BaseModel):
    """``config.contract_seams`` of the orchestrator's contract; no value lives in code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reducer_node: str = Field(min_length=1)
    reducer_operation: str = Field(min_length=1)
    handler_module_template: str = Field(min_length=1)
    handler_class_template: str = Field(min_length=1)
    input_model_template: str = Field(min_length=1)
    emitted_version: dict[str, int]
    emitted_lifecycle: str = Field(min_length=1)
    contract_walker: str = Field(min_length=1)
    drive_timeout_seconds: float = Field(gt=0)
    drive_consumer_group: str = Field(min_length=1)
    golden_chain_expected_fields: tuple[str, ...] = Field(min_length=1)
    golden_chain_census_source: str = Field(min_length=1)


class ModelContractSeamsRunRequest(BaseModel):
    """One design and the empty directory its nodes are scaffolded into."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    design: ModelContractSeamsDesign
    output_root: str = Field(min_length=1)

    @field_validator("output_root")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("output_root must be an absolute path")
        return value


class ModelContractSeamsStepTransition(BaseModel):
    """One fold of a step into the reducer, as the reducer decided it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step: str
    trigger: str
    prior_state: str
    state: str
    moved: bool
    failed_conditions: tuple[str, ...] = ()


class ModelContractSeamsSeamVerdict(BaseModel):
    """node_seam_match_compute's verdict on one crossing, with the field that differed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    crossing_id: str
    verdict: str
    mismatching_field_path: str | None = None


class ModelContractSeamsRunResult(BaseModel):
    """Where the run ended and every reading that put it there."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    design_id: str
    final_state: str
    steps: tuple[str, ...]
    transitions: tuple[ModelContractSeamsStepTransition, ...]
    design_issues: tuple[str, ...] = ()
    scaffolded_nodes: tuple[str, ...] = ()
    seam_verdicts: tuple[ModelContractSeamsSeamVerdict, ...] = ()
    walk_issues: tuple[str, ...] = ()
    chain_event_types: tuple[str, ...] = ()
    chain_states: tuple[str, ...] = ()
    undriven_path_ids: tuple[str, ...] = ()
    sweep_status: str | None = None
    refused_by: str | None = None
    failure: str | None = None


__all__ = [
    "ModelContractSeamsConfig",
    "ModelContractSeamsRunRequest",
    "ModelContractSeamsRunResult",
    "ModelContractSeamsSeamVerdict",
    "ModelContractSeamsStepTransition",
]
