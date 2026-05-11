# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Result model for HandlerCanaryScoreReducer.materialize().

[OMN-10847]
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ModelMaterializeResult(BaseModel, frozen=True):
    """Holds rows destined for both capability_scores and routing_outcomes tables."""

    capability_score_rows: list[dict[str, object]] = Field(default_factory=list)
    routing_outcome_rows: list[dict[str, object]] = Field(default_factory=list)


__all__: list[str] = ["ModelMaterializeResult"]
