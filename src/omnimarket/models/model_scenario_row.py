# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared row produced by a scenario's parity run."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelScenarioRow(BaseModel):
    """One written row. ``provenance`` is validated on its own and left out of the comparison."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    content: str
    provenance: dict[str, str] = Field(default_factory=dict)
