# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output model for node_overseer_observer."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelOverseerObservationResult(BaseModel):
    """Evaluation result for observed side-effect evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    observed_count: int
    evidence_count: int
