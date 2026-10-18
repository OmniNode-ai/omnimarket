# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A heartbeat carries the check; no heartbeat field decides freshness."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelFreshnessTrigger(BaseModel):
    """Foreign heartbeat payloads may grow without breaking this consumer."""

    model_config = ConfigDict(frozen=True, extra="ignore")
    correlation_id: UUID | None = None
