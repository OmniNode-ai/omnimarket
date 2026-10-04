# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Liveness probes: short single-word tasks excluded from judging."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAcceptanceProbeRule(BaseModel):
    """Liveness probes: short single-word tasks excluded from judging."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_task_chars: int = Field(ge=1)
    task_patterns: tuple[str, ...] = Field(min_length=1)
