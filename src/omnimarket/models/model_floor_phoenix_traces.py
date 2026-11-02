# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Phoenix trace reading the factory floor's machinery panel shows (OMN-22097).

One reading of Phoenix's GraphQL API: how many traces it holds and how long ago the newest one ended. A reading that
could not be taken is ``unread`` and says why; it is never a count of zero.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelFloorPhoenixTraces(BaseModel):
    """The trace count and the newest span age of one Phoenix read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["read", "unread"]
    # Sum of traceCount over every Phoenix project; 0 only when state is read.
    trace_count: int = 0
    # End time of the newest trace over every project (Phoenix project.endTime); empty when no trace exists.
    newest_span_at: str = ""
    # Seconds from newest_span_at to the read; None when no trace exists or the read failed.
    newest_span_age_seconds: int | None = None
    read_at: str = ""
    # Why the reading is unread, or what a read without an age lacks. Never names the Phoenix address.
    detail: str = ""
