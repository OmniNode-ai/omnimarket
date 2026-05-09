# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelIngestionSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    total_processed: int
    succeeded: int
    failed: int
    correlation_id: str | None = None
