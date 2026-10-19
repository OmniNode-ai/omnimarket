# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of node_review_record_compute: one review invocation and its arms (OMN-21188)."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_review_execution_status import EnumReviewExecutionStatus
from omnimarket.models.model_review_finding import (
    EnumReviewVerdict,
    ModelReviewFinding,
)
from omnimarket.models.review_record.model_review_record import (
    NO_RUBRIC,
    ModelReviewCost,
    ModelReviewPrincipal,
    ModelReviewServed,
    ModelReviewTarget,
    SettingValue,
)


class ModelReviewArmResult(BaseModel):
    """What one route of an invocation returned."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: str = Field(min_length=1)
    requested_settings: dict[str, SettingValue] = Field(default_factory=dict)
    served: ModelReviewServed = Field(default_factory=ModelReviewServed)
    started_at: AwareDatetime
    finished_at: AwareDatetime
    verdict: EnumReviewVerdict | None = None
    findings: list[ModelReviewFinding] = Field(default_factory=list)
    execution_status: EnumReviewExecutionStatus
    status_reason: str | None = None
    cost: ModelReviewCost

    @model_validator(mode="after")
    def _ordered_and_explained(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at is before started_at")
        completed = self.execution_status is EnumReviewExecutionStatus.COMPLETED
        if not completed and not self.status_reason:
            raise ValueError("a partial or failed arm states its reason")
        return self


class ModelReviewInvocation(BaseModel):
    """One invocation of a reviewer: a target, a template and one or more arms."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: UUID
    source_node: str = Field(min_length=1)
    prompt_template_id: str = Field(min_length=1)
    rubric_hash: str = Field(default=NO_RUBRIC, min_length=1)
    target: ModelReviewTarget
    principal: ModelReviewPrincipal
    arms: list[ModelReviewArmResult] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_arm_per_route(self) -> Self:
        routes = [arm.route for arm in self.arms]
        if len(set(routes)) != len(routes):
            raise ValueError("two arms name the same route; each route is one arm")
        return self


__all__: list[str] = ["ModelReviewArmResult", "ModelReviewInvocation"]
