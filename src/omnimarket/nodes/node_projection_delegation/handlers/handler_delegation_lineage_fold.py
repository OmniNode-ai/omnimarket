# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure fold that reads a delegation's lineage off its terminal (OMN-20606).

Returns the delegation_events columns that name the delegation this one falls
back or escalates from, the kind of relation, and why the parent failed. The
effect writers persist what it returns and decide nothing. A terminal with no
lineage yields no column, so a lineage-less re-emit leaves stored lineage
alone. A malformed lineage is refused by name and yields no column at all, so
it never dead-letters the delegation row and never records half a lineage.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.delegation.delegation_lineage import (
    LINEAGE_KIND_KEY,
    PARENT_CORRELATION_ID_KEY,
    PARENT_FAILURE_CAUSE_KEY,
    ModelDelegationLineage,
    resolve_lineage,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)


class ModelDelegationLineageFold(BaseModel):
    """The outcome of folding a delegation's lineage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lineage: ModelDelegationLineage | None = Field(default=None)
    lineage_refusal: str | None = Field(default=None)

    @model_validator(mode="after")
    def _at_most_one_outcome(self) -> ModelDelegationLineageFold:
        if self.lineage is not None and self.lineage_refusal is not None:
            raise ValueError(
                "a lineage fold holds exactly one of lineage and lineage_refusal"
            )
        return self

    def row_columns(self) -> dict[str, object]:
        """The delegation_events columns this fold names."""
        if self.lineage is None:
            return {}
        return dict(self.lineage.as_columns())


class HandlerDelegationLineageFold:
    """Fold a delegate-skill terminal to its lineage columns."""

    def handle(
        self, request: ModelDelegateSkillTerminalProjection
    ) -> ModelDelegationLineageFold:
        lineage, refusal = resolve_lineage(
            {
                PARENT_CORRELATION_ID_KEY: request.parent_correlation_id,
                LINEAGE_KIND_KEY: request.lineage_kind,
                PARENT_FAILURE_CAUSE_KEY: request.parent_failure_cause,
            },
            own_correlation_id=request.correlation_id,
        )
        if refusal is not None:
            return ModelDelegationLineageFold(lineage_refusal=refusal)
        return ModelDelegationLineageFold(lineage=lineage)


__all__ = ["HandlerDelegationLineageFold", "ModelDelegationLineageFold"]
