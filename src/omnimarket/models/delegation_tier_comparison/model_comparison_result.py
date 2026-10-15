# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The comparison records of one eval run, or the refusals that stopped it."""

from __future__ import annotations

from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    FieldSerializationInfo,
    field_serializer,
    model_validator,
)

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

    @field_serializer("records")
    def _serialize_records(
        self,
        records: tuple[ModelComparisonRecord, ...],
        info: FieldSerializationInfo,
    ) -> list[dict[str, object]]:
        """Keep the fixed record schema even when transport omits optional nulls."""
        return [record.model_dump(mode=info.mode) for record in records]

    @model_validator(mode="after")
    def _records_xor_issues(self) -> Self:
        if self.issues and (self.records or self.underpowered):
            raise ValueError("a refused run carries issues and no records")
        return self
