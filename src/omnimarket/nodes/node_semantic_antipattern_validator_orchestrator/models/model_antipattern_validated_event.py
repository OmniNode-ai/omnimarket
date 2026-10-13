# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Terminal event of one antipattern validation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_semantic_antipattern_validator_orchestrator.models.model_antipattern_classified_result import (
    ModelAntipatternClassifiedViolation,
)


class ModelAntipatternValidatedEvent(BaseModel):
    """What one validate command came to: the violations found, if any.

    match_error is the match effect's non-fatal error (Qdrant or the embedding
    endpoint unavailable, collection missing). When set, zero violations means the
    lookup did not run, not that the file is clean.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    file_path: str
    enforcement_mode: str
    violation_count: int = Field(ge=0)
    has_blocking_violation: bool
    violations: tuple[ModelAntipatternClassifiedViolation, ...] = ()
    match_error: str | None = None


__all__ = ["ModelAntipatternValidatedEvent"]
