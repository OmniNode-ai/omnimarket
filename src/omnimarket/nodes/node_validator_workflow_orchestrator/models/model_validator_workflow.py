# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input, output and effect payloads of node_validator_workflow_orchestrator (OMN-23078)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_validator_verdict import (
    ModelPrVerdict,
    ModelRatchetMove,
    ModelRatchetReading,
    ModelValidatorOutcome,
)


class ModelValidatorWorkflowRequest(BaseModel):
    """One pull request head to validate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    repo: str = Field(min_length=1)
    pr: int = Field(ge=1)
    head: str = Field(min_length=1)
    base: str = Field(min_length=1)


class ModelChangeRead(BaseModel):
    """What the read effect saw: the changed paths and the ratchet readings at the head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    changed_paths: tuple[str, ...]
    ratchet_readings: tuple[ModelRatchetReading, ...] = ()


class EnumSelection(StrEnum):
    SELECTED = "selected"
    NOT_APPLICABLE = "not_applicable"
    NOT_CANONICAL = "not_canonical"
    CONTRACT_UNREADABLE = "contract_unreadable"


class ModelValidatorChoice(BaseModel):
    """One overlay validator and what its contracts said about this change."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    validator_id: str
    selection: EnumSelection
    reason: str = ""
    handler_ref: str = ""
    matched_paths: tuple[str, ...] = ()


class EnumGateConclusion(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


class ModelGateDecision(BaseModel):
    """What the gate effect posts for the pull request head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    pr: int
    head: str
    check_name: str
    conclusion: EnumGateConclusion
    summary: str


class ModelTraceEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    payload: dict[str, object]


class ModelValidatorWorkflowResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: str
    failure: str | None = None
    choices: tuple[ModelValidatorChoice, ...] = ()
    outcomes: tuple[ModelValidatorOutcome, ...] = ()
    verdict: ModelPrVerdict | None = None
    moves: tuple[ModelRatchetMove, ...] = ()
    canonical_validator_count: int = 0
    gate: ModelGateDecision | None = None
