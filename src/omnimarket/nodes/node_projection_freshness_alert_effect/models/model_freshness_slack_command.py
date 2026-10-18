# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Field-for-field wire mirror of ModelSlackPublish; drift tested at the bus seam."""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelFreshnessSlackCommand(BaseModel):
    """No private imports across node boundaries; extra fields fail closed."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    channel: str = Field(..., min_length=1)
    blocks: list[dict[str, Any]] | None = None
    text: str | None = None
    thread_ts: str | None = None
    idempotency_key: str = Field(..., min_length=1)
    correlation_id: UUID
