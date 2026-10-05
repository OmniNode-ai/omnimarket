# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure fold that reads cross-run lineage off a delegate-skill terminal.

Returns the delegation_events columns together or a named refusal. The effect
writers persist what it returns and decide nothing. Absent or malformed
lineage names no columns, so a re-emit leaves stored lineage alone and a bad
value never dead-letters the delegation row.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.delegation.delegation_lineage import (
    attempt_kind_refusal,
    parent_correlation_refusal,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)


class ModelDelegationLineageFold(BaseModel):
    """The outcome of folding a delegation's cross-run lineage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parent_correlation_id: str | None = Field(default=None)
    attempt_kind: str | None = Field(default=None)
    parent_failure_cause: str | None = Field(default=None)
    lineage_refusal: str | None = Field(default=None)

    @model_validator(mode="after")
    def _at_most_one_outcome(self) -> ModelDelegationLineageFold:
        if self.row_columns() and self.lineage_refusal is not None:
            raise ValueError(
                "a lineage fold holds exactly one of lineage columns and "
                "lineage_refusal"
            )
        return self

    def row_columns(self) -> dict[str, object]:
        """The delegation_events columns this fold names."""
        return {
            column: value
            for column, value in (
                ("parent_correlation_id", self.parent_correlation_id),
                ("attempt_kind", self.attempt_kind),
                ("parent_failure_cause", self.parent_failure_cause),
            )
            if value is not None
        }


class HandlerDelegationLineageFold:
    """Fold a delegate-skill terminal to its lineage columns."""

    def handle(
        self, request: ModelDelegateSkillTerminalProjection
    ) -> ModelDelegationLineageFold:
        parent = request.parent_correlation_id
        kind = request.attempt_kind
        cause = request.parent_failure_cause
        if parent is None:
            if kind is not None and kind != "first":
                return ModelDelegationLineageFold(
                    lineage_refusal="attempt_kind requires parent_correlation_id "
                    "unless it is first"
                )
            if cause is not None:
                return ModelDelegationLineageFold(
                    lineage_refusal="parent_failure_cause requires parent_correlation_id"
                )
            return ModelDelegationLineageFold(attempt_kind=kind)

        refusal = parent_correlation_refusal(parent)
        if refusal is not None:
            return ModelDelegationLineageFold(lineage_refusal=refusal)
        if parent == str(request.correlation_id):
            return ModelDelegationLineageFold(
                lineage_refusal="parent_correlation_id must differ from correlation_id"
            )
        refusal = attempt_kind_refusal(kind)
        if refusal is not None:
            return ModelDelegationLineageFold(lineage_refusal=refusal)
        if cause is not None:
            cause = cause.strip()
            if not cause or len(cause) > 256:
                return ModelDelegationLineageFold(
                    lineage_refusal="parent_failure_cause must contain 1 to 256 "
                    "characters after stripping"
                )
        return ModelDelegationLineageFold(
            parent_correlation_id=parent,
            attempt_kind=kind,
            parent_failure_cause=cause,
        )


__all__ = ["HandlerDelegationLineageFold", "ModelDelegationLineageFold"]
