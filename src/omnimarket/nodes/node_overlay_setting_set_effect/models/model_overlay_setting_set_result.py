# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one set did: the value the store held before, the value it holds now, and the two topics written."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, JsonValue

from omnimarket.models.overlay_store.model_overlay_store import OverlayId


class ModelOverlaySettingSetResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay_id: OverlayId
    key: str
    setting_path: str
    actor: str
    previous_present: bool
    previous_value: JsonValue
    new_value: JsonValue
    digest: str
    set_at: datetime
    loaded_topic: str
    audit_topic: str
    # The id the store answered, the revision it committed at and the attempts the set took (a conflict builds it again).
    write_id: str
    revision: int
    attempts: int


__all__ = ["ModelOverlaySettingSetResult"]
