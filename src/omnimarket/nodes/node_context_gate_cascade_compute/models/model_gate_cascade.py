# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded evidence for TE.7; no prompts, model answers or package text."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, pattern=r"^\S+$")]
Cost = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Gate = Literal["kev-9b", "kev-27b", "semif", "laya", "jev"]
Role = Literal["extraction", "selection"]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelGateCascadeObservation(_Frozen):
    """Caller-scored label hits and measured costs for one paired recording."""

    case_id: Identifier
    repeat: Literal[1, 2, 3]
    expected_label_ids: tuple[Identifier, ...]
    ungated_recalled_ids: tuple[Identifier, ...]
    cascade_recalled_ids: tuple[Identifier, ...]
    gate_admitted: bool
    ungated_cost_usd: Cost
    gate_cost_usd: Cost
    downstream_cost_usd: Cost

    @model_validator(mode="after")
    def consistent_recording(self) -> Self:
        for values in (
            self.expected_label_ids,
            self.ungated_recalled_ids,
            self.cascade_recalled_ids,
        ):
            if len(values) != len(set(values)):
                raise ValueError("duplicate label id")
        expected = set(self.expected_label_ids)
        if not set(self.ungated_recalled_ids) <= expected:
            raise ValueError("ungated hit is not an expected label")
        if not set(self.cascade_recalled_ids) <= expected:
            raise ValueError("cascade hit is not an expected label")
        if not self.gate_admitted and (
            self.cascade_recalled_ids or self.downstream_cost_usd != 0
        ):
            raise ValueError("skipped downstream cannot have recall hits or cost")
        return self


class ModelGateCascadeRequest(_Frozen):
    profile_id: Identifier
    role: Role
    gate: Gate
    winner_rung_id: Identifier
    winner_evidence_id: Identifier
    label_set_id: Identifier
    gate_training_evidence_id: Identifier | None = None
    work_unit_repository: Annotated[str, Field(pattern=r"^[\w.-]+/[\w.-]+$")]
    repository_visibility: Literal["public", "private", "unknown"]
    case_ids: tuple[Identifier, ...] = Field(min_length=1)
    observations: tuple[ModelGateCascadeObservation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def complete_attributed_grid(self) -> Self:
        if self.gate == "jev" and self.repository_visibility != "public":
            raise ValueError("Jev requires public repository attribution")
        if self.gate == "laya" and self.gate_training_evidence_id is None:
            raise ValueError("Laya requires label-bound training evidence")
        if len(self.case_ids) != len(set(self.case_ids)):
            raise ValueError("duplicate case id")
        keys = [(row.case_id, row.repeat) for row in self.observations]
        expected = {(case, repeat) for case in self.case_ids for repeat in (1, 2, 3)}
        if len(keys) != len(set(keys)) or set(keys) != expected:
            raise ValueError("requires exactly one observation per case and repeat")
        labels: dict[str, frozenset[str]] = {}
        for row in self.observations:
            current = frozenset(row.expected_label_ids)
            if row.case_id in labels and labels[row.case_id] != current:
                raise ValueError("expected labels differ between repeats")
            labels[row.case_id] = current
        return self


class ModelGateCascadeCaseResult(_Frozen):
    case_id: str
    repeat: int
    expected_count: int
    ungated_recalled_count: int
    cascade_recalled_count: int
    ungated_recall: float | None
    cascade_recall: float | None
    recall_equal: bool
    gate_admitted: bool
    ungated_cost_usd: Decimal
    cascade_cost_usd: Decimal
    cost_saved_usd: Decimal


class ModelGateCascadeResult(_Frozen):
    profile_id: str
    role: Role
    gate: Gate
    winner_rung_id: str
    winner_evidence_id: str
    label_set_id: str
    gate_training_evidence_id: str | None
    observations: tuple[ModelGateCascadeCaseResult, ...]
    recall_equal_everywhere: bool
    ungated_cost_usd: Decimal
    cascade_cost_usd: Decimal
    cost_saved_usd: Decimal
    recommended: bool
