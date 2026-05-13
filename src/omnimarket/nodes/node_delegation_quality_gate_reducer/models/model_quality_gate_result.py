# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Quality gate result model for the delegation pipeline.

Represents the output of the quality gate reducer: pass/fail,
quality score, failure reasons, fail category, and fallback recommendation.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# Discriminated fail category:
# - "pass": all checks passed
# - "fail_deterministic": a deterministic DoD check failed — BLOCKS delegation
# - "fail_heuristic": only heuristic checks failed — escalate per contract policy
EnumQualityGateCategory = Literal["pass", "fail_deterministic", "fail_heuristic"]


class ModelQualityGateResult(BaseModel):
    """Gate output: pass/fail verdict, score, fail category, failure reasons, and fallback flag.

    Attributes:
        correlation_id: Tracks this result back to the original request.
        passed: Whether the LLM response passed the quality gate.
        fail_category: Structured outcome: "pass", "fail_deterministic" (hard block),
            or "fail_heuristic" (escalate per contract policy). When contract DoD
            checks are not provided, falls back to legacy heuristic-only mode and
            this field reflects that outcome.
        quality_score: Quality score from 0.0 to 1.0.
        failure_reasons: Tuple of human-readable failure reason strings.
        fallback_recommended: Whether fallback to Claude is recommended.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: UUID = Field(
        ...,
        description="Tracks this result back to the original request.",
    )
    passed: bool = Field(
        ...,
        description="Whether the LLM response passed the quality gate.",
    )
    fail_category: EnumQualityGateCategory = Field(
        default="pass",
        description=(
            "Structured outcome: 'pass', 'fail_deterministic' (hard block), "
            "or 'fail_heuristic' (escalate per contract policy)."
        ),
    )
    quality_score: float = Field(
        ...,
        description="Quality score from 0.0 to 1.0.",
    )
    failure_reasons: tuple[str, ...] = Field(
        default=(),
        description="Tuple of human-readable failure reason strings.",
    )
    fallback_recommended: bool = Field(
        default=False,
        description="Whether fallback to Claude is recommended.",
    )


__all__: list[str] = ["EnumQualityGateCategory", "ModelQualityGateResult"]
