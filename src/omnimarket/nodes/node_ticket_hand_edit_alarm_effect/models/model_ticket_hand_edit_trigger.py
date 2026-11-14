# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A heartbeat carries the trigger; no heartbeat field decides what is a hand edit."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelTicketHandEditSweepTrigger(BaseModel):
    """Foreign heartbeat payloads may grow without breaking this consumer."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: UUID | None = None
