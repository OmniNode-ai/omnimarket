# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request the floor panel answers: the panel it is addressed to."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelFloorPanelRequest(BaseModel):
    """The request the floor panel answers: the panel it is addressed to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    panel: str = Field(min_length=1)


__all__ = ["ModelFloorPanelRequest"]
