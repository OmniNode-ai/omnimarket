# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the floor's placement reader made of the host capacity advertisement file (OMN-23435).

``ModelHostCapacityAdvertisement`` is strict for its writers. The floor only reads it, so a field the reader's
revision does not know is a degraded reading, never a reason to refuse the whole file.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)


class ModelFloorPlacementReading(BaseModel):
    """The advertisements that were read, and what the reader had to leave out to read them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hosts: tuple[ModelHostCapacityAdvertisement, ...] = ()
    #: ``<host_name>.<field>`` of every field on a row that this revision's model does not declare; the reader ignored it.
    unknown_fields: tuple[str, ...] = ()
    #: ``<host_name or row index>: <reason>`` of every row the reader skipped because it failed validation.
    unreadable_rows: tuple[str, ...] = ()

    @property
    def cause(self) -> str:
        """Empty when nothing was left out, else one sentence naming what was."""
        parts: list[str] = []
        if self.unknown_fields:
            parts.append(f"ignored unknown field(s) {', '.join(self.unknown_fields)}")
        if self.unreadable_rows:
            parts.append(f"skipped unreadable row(s) {'; '.join(self.unreadable_rows)}")
        return "; ".join(parts)
