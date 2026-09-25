# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One entry of the check register: a check and its declared class."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.ranges.enum_check_class import EnumCheckClass
from omnimarket.models.ranges.enum_range_status import EnumRangeStatus
from omnimarket.models.ranges.model_range_acceptance_line import (
    ModelRangeAcceptanceLine,
)


class ModelCheckDeclaration(BaseModel):
    """A check, its class, and for a range its line or its NOT SET status.

    A gate carries no range line; a numeric rule on a gate that makes no
    statistical claim is labelled in ``policy_threshold``. A range declares
    ``range_status``: ``declared`` with a full acceptance line, or ``not_set``
    with none. A check that mixes both kinds is registered as two entries, the
    deterministic half as a gate and the sampled half as a range.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str = Field(..., min_length=1)
    check_class: EnumCheckClass
    blocks: tuple[str, ...] = Field(
        default=(), description="The surfaces this check blocks."
    )
    range_status: EnumRangeStatus | None = None
    acceptance_line: ModelRangeAcceptanceLine | None = None
    policy_threshold: str | None = Field(
        default=None,
        description="A deterministic numeric rule with a fixed count and no statistical claim.",
    )
    source: str = Field(
        default="", description="Where this classification was made or evidenced."
    )

    @model_validator(mode="after")
    def _class_shape(self) -> Self:
        if self.check_class is EnumCheckClass.GATE:
            if self.range_status is not None or self.acceptance_line is not None:
                raise ValueError(
                    f"{self.check_id}: a gate carries no range_status or acceptance_line"
                )
            return self
        if self.policy_threshold is not None:
            raise ValueError(
                f"{self.check_id}: a range is never a policy threshold; "
                "declare its line or mark it not_set"
            )
        if self.range_status is None:
            raise ValueError(
                f"{self.check_id}: a range declares range_status "
                "(declared with an acceptance_line, or not_set)"
            )
        if self.range_status is EnumRangeStatus.DECLARED:
            if self.acceptance_line is None:
                raise ValueError(
                    f"{self.check_id}: a declared range carries its acceptance_line"
                )
            if self.acceptance_line.check_id != self.check_id:
                raise ValueError(
                    f"{self.check_id}: acceptance_line.check_id "
                    f"{self.acceptance_line.check_id!r} names another check"
                )
        elif self.acceptance_line is not None:
            raise ValueError(
                f"{self.check_id}: a not_set range carries no acceptance_line"
            )
        return self
