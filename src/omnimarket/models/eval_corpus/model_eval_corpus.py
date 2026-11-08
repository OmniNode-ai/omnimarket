# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed rows of the eval corpus and the J15 join (piece 15 to piece 16, OMN-22978).

A delegated result and its cost are two rows from two sources. The corpus holds one entry per delegated run:
the result, and either the cost row or the typed word that no cost row exists. A result is never dropped
because its cost is missing, and a cost row is never invented for it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: The floor's join fold reads a failure that never started as a refusal only when its code carries this prefix
#: (node_floor_emission_compute, ``UPSTREAM_PREFIX``).
UPSTREAM_FAILURE_PREFIX = "upstream_"


class EnumCorpusCostStatus(StrEnum):
    PRESENT = "present"
    MISSING = "missing"


class ModelDelegatedResult(BaseModel):
    """What piece 15 returned for one delegated run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    task_class: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
    result_summary: str


class ModelDelegationCost(BaseModel):
    """What piece 15 measured as the cost of one delegated run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    cost_usd: Decimal = Field(ge=Decimal("0"))
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    measurement_source: str = Field(min_length=1)


class ModelEvalCorpusEntry(BaseModel):
    """One corpus row: the result, and its cost or the typed word that it is missing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    task_class: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
    result_summary: str
    cost_status: EnumCorpusCostStatus
    cost: ModelDelegationCost | None = None
    joined_at: datetime

    @model_validator(mode="after")
    def _cost_matches_status(self) -> ModelEvalCorpusEntry:
        if self.cost_status is EnumCorpusCostStatus.PRESENT and self.cost is None:
            raise ValueError("a corpus entry typed present carries its cost row")
        if self.cost_status is EnumCorpusCostStatus.MISSING and self.cost is not None:
            raise ValueError("a corpus entry typed missing carries no cost row")
        if self.cost is not None and self.cost.run_id != self.run_id:
            raise ValueError("the cost row belongs to another run")
        return self


class ModelCorpusReadRequest(BaseModel):
    """Read the corpus; no run ids reads every entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_ids: tuple[str, ...] = ()


class ModelCorpusReadResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[ModelEvalCorpusEntry, ...]
    #: Run ids whose entry reads cost missing, in corpus order.
    missing_cost_run_ids: tuple[str, ...]
    #: Requested run ids with no entry in the corpus, in request order.
    absent_run_ids: tuple[str, ...]
    #: Journal lines the reader could not parse; counted, never repaired.
    skipped_lines: int = Field(ge=0)


class EnumJoinJ15Outcome(StrEnum):
    LANDED = "landed"
    REFUSED_NO_DELEGATED_RESULT = "refused_no_delegated_result"
    COST_MISSING = "cost_missing"
    ENTRY_DROPPED = "entry_dropped"


class ModelJoinJ15Policy(BaseModel):
    """The contract's ``join_j15`` section: nothing here is spelled in the handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    join_id: str = Field(pattern=r"^J\d{1,2}$")
    from_piece: int = Field(ge=1, le=18)
    to_piece: int = Field(ge=1, le=18)
    emission_node: str = Field(min_length=1)
    no_result_failure_code: str = Field(min_length=1)
    cost_missing_failure_code: str = Field(min_length=1)
    entry_dropped_failure_code: str = Field(min_length=1)

    @field_validator("no_result_failure_code", "cost_missing_failure_code")
    @classmethod
    def _upstream_shape(cls, value: str) -> str:
        if not value.startswith(UPSTREAM_FAILURE_PREFIX):
            raise ValueError(
                f"the no-result and cost-missing codes must start with {UPSTREAM_FAILURE_PREFIX!r}: "
                "the floor reads a failure that never started as a join refusal only then"
            )
        return value


class ModelJoinJ15Request(BaseModel):
    """Join the delegated runs named by id into the corpus, under piece 15's correlation id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    run_ids: tuple[str, ...] = Field(min_length=1)


class ModelJoinJ15Result(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumJoinJ15Outcome
    correlation_id: str
    failure_code: str | None = None
    detail: str = ""
    #: What the corpus reader returned for the requested runs after the join wrote them.
    entries: tuple[ModelEvalCorpusEntry, ...] = ()
    missing_cost_run_ids: tuple[str, ...] = ()
    absent_run_ids: tuple[str, ...] = ()


__all__ = [
    "UPSTREAM_FAILURE_PREFIX",
    "EnumCorpusCostStatus",
    "EnumJoinJ15Outcome",
    "ModelCorpusReadRequest",
    "ModelCorpusReadResult",
    "ModelDelegatedResult",
    "ModelDelegationCost",
    "ModelEvalCorpusEntry",
    "ModelJoinJ15Policy",
    "ModelJoinJ15Request",
    "ModelJoinJ15Result",
]
