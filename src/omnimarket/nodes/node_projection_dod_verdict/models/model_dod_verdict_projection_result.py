# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output of the pure definition-of-done verdict fold (def-B response)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_dod_verdict.models.model_dod_eval_verdict import (
    ModelDodEvalVerdict,
)
from omnimarket.nodes.node_projection_dod_verdict.models.model_dod_verdict_row import (
    ModelDodVerdictRow,
)


class ModelDodVerdictProjectionResult(BaseModel):
    """The row the writer should upsert, and the eval verdict it carries.

    ``row`` is ``None`` exactly when the event is a rehearsal (OMN-18901). The
    decision is the fold's, not the writer's: what is worth storing is part of
    the projection's logic, and keeping it in the pure half means a unit test
    can falsify it without a database while the writer only persists what the
    fold hands it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelDodVerdictRow | None = Field(
        ...,
        description=(
            "The run row to upsert, or None for a rehearsal (dry_run), which "
            "is not an attempt and is not projected."
        ),
    )
    verdict: ModelDodEvalVerdict = Field(
        ...,
        description=(
            "The same answer the row's own outcome columns carry, returned "
            "as the typed object so a caller folding in memory gets the "
            "refusal without reconstructing it from two columns."
        ),
    )


__all__ = ["ModelDodVerdictProjectionResult"]
