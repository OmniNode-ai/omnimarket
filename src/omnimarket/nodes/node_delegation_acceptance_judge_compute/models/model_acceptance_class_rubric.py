# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Accept criteria and reject notes for one task class."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAcceptanceClassRubric(BaseModel):
    """Accept criteria and reject notes for one task class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    routing_labels: tuple[str, ...]
    accept: tuple[str, ...] = Field(min_length=1)
    reject_notes: tuple[str, ...] = Field(min_length=1)
