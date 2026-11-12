# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A heartbeat carries the sweep trigger; no heartbeat field decides anything."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelMergeOverrideSweepTrigger(BaseModel):
    """Foreign heartbeat payloads may grow without breaking this consumer."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: UUID | None = None
