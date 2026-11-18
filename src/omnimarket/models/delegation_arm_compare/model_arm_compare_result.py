# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The answer of one paired arm comparison, or the typed failure that stopped it."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.models.delegation_arm_compare.enum_arm_compare_failure import (
    EnumArmCompareFailure,
)
from omnimarket.models.ranges.model_comparison_result import ModelComparisonResult


class ModelArmCompareResult(BaseModel):
    """The measured comparison, its negative control, or the failure.

    ``result`` is the exact paired verdict of the two arms; ``control`` is the
    same test on the negative control, present only when the run had one. A
    result holds either a verdict or a failure, never both and never neither:
    a failure never carries a verdict.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    comparison_id: str
    result: ModelComparisonResult | None = None
    control: ModelComparisonResult | None = None
    control_cancelled_gain: bool = False
    failure: EnumArmCompareFailure | None = None
    detail: str = ""

    @model_validator(mode="after")
    def _result_xor_failure(self) -> Self:
        if self.result is not None and self.failure is not None:
            raise ValueError("a failed comparison carries no verdict")
        if self.result is None and self.failure is None:
            raise ValueError("a comparison holds a result or a failure, not neither")
        return self
