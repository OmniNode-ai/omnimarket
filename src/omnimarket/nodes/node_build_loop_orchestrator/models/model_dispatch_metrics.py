# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from pydantic import BaseModel


class DispatchMetrics(BaseModel):
    metrics_id: str
    dispatch_count: int
    last_dispatched: str | None = None
