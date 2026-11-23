# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The audit row of one set: who set which declared key, the value before and the value after."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, JsonValue

from omnimarket.models.overlay_store.model_overlay_store import OverlayId


class ModelOverlaySettingAudit(BaseModel):
    """Only declared settings are settable, and a declared setting holds no secret, so the values ride in the row:
    the previous value is what a restore sets back."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay_id: OverlayId
    key: str
    setting_path: str
    actor: str
    previous_present: bool
    previous_value: JsonValue
    new_value: JsonValue
    document_digest: str
    set_at: datetime


__all__ = ["ModelOverlaySettingAudit"]
