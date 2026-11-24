# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One overlay's current record as the settings store holds it: the loaded overlay plus its revision and heartbeat.

Records published before revisions and heartbeats existed carry neither; the store skips and counts them.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class ModelOverlaySettingsRecord(BaseModel):
    """The record the topic carries and Valkey keeps for one overlay."""

    # The topic may carry more than this reader needs (provenance, the audit pointer): those fields are not its business.
    model_config = ConfigDict(frozen=True, extra="allow")

    overlay_id: str = Field(min_length=1)
    revision: int | None = Field(default=None, ge=1)
    heartbeat_at: datetime | None = None
    document: dict[str, JsonValue]


__all__ = ["ModelOverlaySettingsRecord"]
