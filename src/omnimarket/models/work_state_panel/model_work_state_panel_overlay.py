# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The panel overlay: the panels the floor answers, each bound to its own projector and topic."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.work_state_panel.model_work_state_panel_entry import (
    ModelWorkStatePanelEntry,
)
from omnimarket.models.work_state_panel.model_work_state_panel_row_reader_route import (
    ModelWorkStatePanelRowReaderRoute,
)


class ModelWorkStatePanelOverlay(BaseModel):
    """The declared panels: unique ids, and no two entries binding the same topic to the same projector."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    panels: tuple[ModelWorkStatePanelEntry, ...] = Field(min_length=1)
    row_reader: ModelWorkStatePanelRowReaderRoute | None = Field(
        default=None,
        description="When set, the answer node reads the row from the database this route names; when None it reads the in-memory table (the default profile).",
    )

    @model_validator(mode="after")
    def _panels_are_unique(self) -> Self:
        seen_ids: set[str] = set()
        seen_bindings: set[tuple[str, str]] = set()
        for entry in self.panels:
            if entry.panel_id in seen_ids:
                raise ValueError("duplicate panel_id")
            binding = (entry.topic, entry.projector.value)
            if binding in seen_bindings:
                raise ValueError("duplicate panel_id")
            seen_ids.add(entry.panel_id)
            seen_bindings.add(binding)
        return self

    def entry_for(self, panel_id: str) -> ModelWorkStatePanelEntry | None:
        """The entry with that id, or None when the overlay does not declare it."""
        for entry in self.panels:
            if entry.panel_id == panel_id:
                return entry
        return None


__all__ = ["ModelWorkStatePanelOverlay"]
