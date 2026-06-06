# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for node_overseer_observer."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelOverseerObservationRequest(BaseModel):
    """Observed side effects and required evidence for evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dod_evidence: list[dict[str, Any]] = Field(default_factory=list)
    observed: list[dict[str, Any]] = Field(default_factory=list)
