# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Three-valued verdict with aggregation invariants enforced at validation."""

from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.nodes.node_delegation_rubric_check_compute.models.enum_rubric_outcome import (
    EnumRubricOutcome,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_rubric_criterion_result import (
    ModelRubricCriterionResult,
)


class ModelRubricVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_class: str
    rubric_version: str
    criteria: tuple[ModelRubricCriterionResult, ...]
    outcome: EnumRubricOutcome
    failed_criteria: tuple[str, ...]

    @model_validator(mode="after")
    def consistent_aggregate(self) -> Self:
        failed = tuple(
            row.criterion_id
            for row in self.criteria
            if row.outcome == EnumRubricOutcome.FAIL
        )
        expected = (
            EnumRubricOutcome.FAIL
            if failed
            else EnumRubricOutcome.PASS
            if any(row.outcome == EnumRubricOutcome.PASS for row in self.criteria)
            else EnumRubricOutcome.UNDETERMINED
        )
        if self.outcome != expected or self.failed_criteria != failed:
            raise ValueError("verdict must agree with recorded criterion outcomes")
        return self
