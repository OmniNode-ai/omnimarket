# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Input model for dispatch outcome bridge runtime routing."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelDispatchOutcomeBridgeInput(BaseModel):
    """Dispatch-worker completion payload accepted by the bridge effect."""

    model_config = ConfigDict(extra="allow")

    task_id: str
    dispatch_id: str
    ticket_id: str | None = None
    status: str = "error"
    artifact_path: str | None = None
    model_calls: list[dict[str, Any]] = Field(default_factory=list)
    token_cost: int = 0
    dollars_cost: float = 0.0
    cost_provenance: dict[str, Any] = Field(default_factory=dict)


__all__ = ["ModelDispatchOutcomeBridgeInput"]
