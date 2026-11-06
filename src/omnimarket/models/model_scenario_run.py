# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared result of running a parity scenario under one binding profile."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_scenario_row import ModelScenarioRow


class ModelScenarioRun(BaseModel):
    """What one scenario produced under one profile."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str
    binding: str
    proof_eligible: bool
    verdicts: tuple[str, ...] = ()
    lines: tuple[str, ...] = ()
    rows: tuple[ModelScenarioRow, ...] = ()
    counts: dict[str, int] = Field(default_factory=dict)
    provenance: dict[str, str] = Field(default_factory=dict)
