# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One key named in the alert."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict


class ModelKeyPoolAlertKey(BaseModel):
    """Name and reference of a key, with its expiry when it has one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    api_key_ref: str
    expires_at: AwareDatetime | None = None
    days_remaining: int | None = None


__all__ = ["ModelKeyPoolAlertKey"]
