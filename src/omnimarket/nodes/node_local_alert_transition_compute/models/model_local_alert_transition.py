# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local alert transition compute's request and result."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.model_local_alert_transitioned import (
    ModelLocalAlertTransitioned,
)


class ModelLocalAlertTransitionRequest(BaseModel):
    """A tenant's local_environment rows before and after one write, as stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    before: tuple[dict[str, object], ...]
    after: tuple[dict[str, object], ...]


class ModelLocalAlertTransitionResult(BaseModel):
    """The raises and clears that write made, clears first for each row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transitions: tuple[ModelLocalAlertTransitioned, ...]


__all__ = ["ModelLocalAlertTransitionRequest", "ModelLocalAlertTransitionResult"]
