# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from dataclasses import dataclass

dataclass


class ModelLoopCycleSummary:
    orchestrator_id: str
    cycle_id: str
    start_time: str
    end_time: str | None = None
    status: str | None = None
    metrics: dict | None = None
