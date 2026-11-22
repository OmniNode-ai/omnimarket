# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The input of the terminal writer: one delegation terminal event as delivered."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelDelegationTerminalEnvelope(BaseModel):
    """One delegation-completed or delegation-failed delivery.

    ``topic`` is the canonical topic name the delivery arrived on; ``payload`` is the flat
    ModelDelegationResult wire object. It is kept as a plain mapping because validation
    of every field, with the dead-letter precedence, is the handler's job.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    topic: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


__all__: list[str] = ["ModelDelegationTerminalEnvelope"]
