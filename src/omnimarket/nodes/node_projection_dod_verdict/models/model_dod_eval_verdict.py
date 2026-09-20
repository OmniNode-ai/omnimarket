# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The eval metric's done predicate, as a typed answer rather than a boolean."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_projection_dod_verdict.models.enum_dod_eval_outcome import (
    EnumDodEvalOutcome,
)
from omnimarket.nodes.node_projection_dod_verdict.models.enum_dod_eval_refusal import (
    EnumDodEvalRefusal,
)


class ModelDodEvalVerdict(BaseModel):
    """Whether one verification counts as done, and if not, why not.

    The pairing is enforced rather than documented, in the shape the verify
    node already uses for its own unresolved-cause field: a refusal with no
    reason is unactionable, and a reason beside a DONE outcome misreports a
    run that did count. Both are rejected at construction, so a caller cannot
    build the ambiguous object in the first place.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumDodEvalOutcome = Field(..., description="Done, or refused.")
    refusal: EnumDodEvalRefusal | None = Field(
        default=None,
        description="Which conjunct failed. Set exactly when outcome is refused.",
    )

    @model_validator(mode="after")
    def _refusal_pairs_with_refused(self) -> Self:
        """A refusal names its reason, and a done outcome names none."""
        refused = self.outcome is EnumDodEvalOutcome.REFUSED
        if refused != (self.refusal is not None):
            raise ValueError(
                "refusal is set exactly when outcome is REFUSED; got "
                f"outcome={self.outcome.value}, refusal={self.refusal!r}"
            )
        return self

    @property
    def is_done(self) -> bool:
        """True only for DONE. Provided so no caller re-spells the comparison."""
        return self.outcome is EnumDodEvalOutcome.DONE


__all__ = ["ModelDodEvalVerdict"]
