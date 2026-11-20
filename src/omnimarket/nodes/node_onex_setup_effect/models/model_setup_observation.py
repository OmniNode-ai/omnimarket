# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pydantic model for a single setup step observation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .model_onex_setup import ModelOnexSetupReceipt


class ModelSetupObservation(BaseModel):
    """Observation of a single setup step."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step: str
    status: Literal["success", "failure", "action_required", "skipped", "unavailable"]
    changed: bool = False
    evidence: dict[str, str] = Field(default_factory=dict)
    fix: str | None = None
    retry: str | None = None
    receipts: tuple[ModelOnexSetupReceipt, ...] = ()
