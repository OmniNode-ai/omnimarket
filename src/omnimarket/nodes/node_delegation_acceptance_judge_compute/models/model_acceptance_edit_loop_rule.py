# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How a turn of the tool-driven editing loop is recognised."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAcceptanceEditLoopRule(BaseModel):
    """How a turn of the tool-driven editing loop is recognised."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    task_patterns: tuple[str, ...] = Field(min_length=1)
