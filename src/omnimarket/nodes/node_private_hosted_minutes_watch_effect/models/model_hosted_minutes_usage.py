# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Billed hosted minutes and workflow evidence for one repository and UTC day."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict


class ModelRepoDayMinutes(BaseModel):
    """Aggregated hosted minutes charged to private repository usage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    day: date
    minutes: float
    skus: tuple[str, ...]
    workflows: tuple[str, ...]
