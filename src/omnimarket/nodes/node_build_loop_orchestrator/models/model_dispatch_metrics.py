# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from pydantic import BaseModel


class DispatchMetrics(BaseModel):
    metrics_id: str
    metric_type: str
    value: float
    recorded_at: str
