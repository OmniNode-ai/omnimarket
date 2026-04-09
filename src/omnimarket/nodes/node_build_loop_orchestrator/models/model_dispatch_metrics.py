# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from dataclasses import dataclass

dataclass


class ModelDispatchMetrics:
    orchestrator_id: str
    metrics_data: dict
    recorded_at: str
    source: str | None = None
