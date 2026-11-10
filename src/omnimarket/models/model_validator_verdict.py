# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed state, input and output of node_validator_verdict_reducer (OMN-23078).

The reducer folds one pull request's validator outcomes and ratchet readings into that
pull request's verdict, the ratchet baselines (one-way state) and the floor's count of
canonical validators that have run.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumRatchetDirection(StrEnum):
    """The one way a ratchet baseline may move."""

    SHRINK_ONLY = "shrink_only"
    GROW_ONLY = "grow_only"


class EnumPrVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class ModelValidatorOutcome(BaseModel):
    """What one validator's report said about one pull request head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    validator_id: str = Field(min_length=1)
    canonical: bool
    failing_count: int = Field(ge=0)
    rule_ids: tuple[str, ...] = ()
    error: str | None = None


class ModelRatchetReading(BaseModel):
    """A ratchet value read at a pull request head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    value: int = Field(ge=0)


class ModelRatchetBaseline(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    direction: EnumRatchetDirection
    value: int = Field(ge=0)


class ModelRatchetRefusal(BaseModel):
    """A reading that would move a baseline the wrong way, or that names no declared ratchet."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    reason: str
    baseline: int | None = None
    proposed: int


class ModelRatchetMove(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    before: int | None
    after: int


class ModelPrVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    pr: int
    head: str
    verdict: EnumPrVerdict
    failing_validators: tuple[str, ...] = ()
    errored_validators: tuple[str, ...] = ()
    refused_ratchets: tuple[ModelRatchetRefusal, ...] = ()


class ModelVerdictState(BaseModel):
    """The reducer's whole state, held by the injected verdict store."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdicts: dict[str, ModelPrVerdict] = Field(default_factory=dict)
    baselines: dict[str, ModelRatchetBaseline] = Field(default_factory=dict)
    canonical_validators: tuple[str, ...] = ()

    @property
    def canonical_validator_count(self) -> int:
        return len(self.canonical_validators)


class ModelVerdictFoldRequest(BaseModel):
    """One pull request head's outcomes and readings, to fold into the state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    repo: str = Field(min_length=1)
    pr: int = Field(ge=1)
    head: str = Field(min_length=1)
    outcomes: tuple[ModelValidatorOutcome, ...]
    ratchet_readings: tuple[ModelRatchetReading, ...] = ()


class ModelVerdictFoldResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    verdict: ModelPrVerdict
    moves: tuple[ModelRatchetMove, ...] = ()
    canonical_validator_count: int = Field(ge=0)
    state: ModelVerdictState


def pr_key(repo: str, pr: int) -> str:
    return f"{repo}#{pr}"
