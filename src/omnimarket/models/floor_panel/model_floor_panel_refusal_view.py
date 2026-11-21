# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The refusal the panel recorded last: its code, its time and the refusal id that names it."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ModelFloorPanelRefusalView(BaseModel):
    """The refusal the answer names: the code, when it happened and the id that pins it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    at: datetime | None = None
    refusal_id: str | None = None


__all__ = ["ModelFloorPanelRefusalView"]
