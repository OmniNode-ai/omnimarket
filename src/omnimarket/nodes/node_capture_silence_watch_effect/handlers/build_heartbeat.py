# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the tick's own progress heartbeat without reading a clock."""

from datetime import datetime

from omnimarket.models.liveness.model_automation_liveness import (
    ModelAutomationHeartbeat,
)


def build_heartbeat(
    *,
    process_id: str,
    host: str,
    process_started_at: datetime,
    now: datetime,
    progress_counter: int,
    demand_count: int,
    pid: int | None = None,
) -> ModelAutomationHeartbeat:
    """The caller supplies process identity and progress; no watchdog leg fields."""
    return ModelAutomationHeartbeat(
        process_id=process_id,
        host=host,
        pid=pid,
        process_started_at=process_started_at,
        progress_counter=progress_counter,
        last_progress_at=now,
        demand_count=demand_count,
        emitted_at=now,
    )
