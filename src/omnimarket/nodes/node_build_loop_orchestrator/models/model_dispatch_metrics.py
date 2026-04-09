# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from pydantic import BaseModel


class DispatchMetrics(BaseModel):
    metrics_id: str
    dispatched_count: int
    timestamp: str | None = None
