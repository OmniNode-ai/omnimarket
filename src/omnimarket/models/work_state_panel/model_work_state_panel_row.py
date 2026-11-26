# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The panel's persisted row: the held publication, or the last refusal that kept it empty."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.work_state_panel.enum_panel_refusal import EnumPanelRefusal
from omnimarket.models.work_state_panel.enum_panel_state import EnumPanelRowState
from omnimarket.models.work_state_panel.model_work_state_panel_body import (
    ModelWorkStatePanelBody,
)


class ModelWorkStatePanelRow(BaseModel):
    """The persisted payload: HELD while it holds a publication, EMPTY while it only records its last refusal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    epoch: int = Field(default=0, ge=0)
    as_of: datetime | None = None
    publication_id: str | None = None
    body: ModelWorkStatePanelBody | None = None
    received_at: datetime | None = None
    last_refusal_code: EnumPanelRefusal | None = None
    last_refusal_at: datetime | None = None
    last_refusal_id: str | None = None

    @field_validator("last_refusal_id")
    @classmethod
    def _refusal_id_shape(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"refused-[0-9a-f]{64}", value):
            raise ValueError("last_refusal_id must match ^refused-[0-9a-f]{64}$")
        return value

    @property
    def state(self) -> EnumPanelRowState:
        """HELD while the row holds a publication, EMPTY while it only records its last refusal."""
        return (
            EnumPanelRowState.HELD
            if self.publication_id is not None
            else EnumPanelRowState.EMPTY
        )


__all__ = ["ModelWorkStatePanelRow"]
