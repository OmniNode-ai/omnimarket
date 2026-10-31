# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT


"""Structured result from a sweep skill run.

Observability/telemetry type — lives with the aislop sweep node that emits it, not overseer.
Emitted by sweep nodes after each run and projected into the sweep_results table
by the omnidash read-model consumer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelSweepResult(BaseModel):
    """Structured result from a sweep skill run.

    Observability/telemetry type — lives with the aislop sweep node that emits it, not overseer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "1.0.0"
    sweep_type: Literal[
        "aislop",
        "coverage",
        "compliance",
        "contract",
        "dashboard",
        "runtime",
        "data_flow",
    ]
    session_id: str
    correlation_id: str
    ran_at: datetime
    duration_seconds: float
    passed: bool
    finding_count: int = 0
    critical_count: int = 0
    warning_count: int = 0
    repos_scanned: tuple[str, ...] = Field(default_factory=tuple)
    summary: str
    output_path: str | None = None  # path to full JSON output
