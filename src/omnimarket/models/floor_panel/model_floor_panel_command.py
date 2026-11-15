# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The loose envelope the floor panel answers: the correlation id and the request's payload as published."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelFloorPanelCommand(BaseModel):
    """The envelope as it crossed the bus: the payload is carried through, not re-validated here."""

    model_config = ConfigDict(frozen=True, extra="allow")

    correlation_id: str = Field(min_length=1)
    payload: dict[str, object] = Field(default_factory=dict)


__all__ = ["ModelFloorPanelCommand"]
