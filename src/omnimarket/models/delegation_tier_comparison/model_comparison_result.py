# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The comparison records of one eval run, or the refusals that stopped it."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.models.delegation_tier_comparison.model_comparison_issue import (
    ModelComparisonIssue,
)
from omnimarket.models.delegation_tier_comparison.model_comparison_record import (
    ModelComparisonRecord,
)
from omnimarket.models.delegation_tier_comparison.model_comparison_underpowered import (
    ModelComparisonUnderpowered,
)


class ModelComparisonResult(BaseModel):
    """All records of the run or none of them and the issues."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    records: tuple[ModelComparisonRecord, ...] = ()
    underpowered: tuple[ModelComparisonUnderpowered, ...] = ()
    issues: tuple[ModelComparisonIssue, ...] = ()

    @model_validator(mode="after")
    def _records_xor_issues(self) -> Self:
        if self.issues and (self.records or self.underpowered):
            raise ValueError("a refused run carries issues and no records")
        return self
