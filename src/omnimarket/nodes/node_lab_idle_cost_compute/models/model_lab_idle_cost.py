# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the lab-idle cost sum (OMN-22885)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelLabIdleCostRequest(BaseModel):
    """Ledger rows and the start to sum from (an ISO stamp, inclusive).

    since: empty means the last orchestrator CLAIM in rows (else the first row given), so a
    caller that only has the ledger asks for "since the last start" without reading it twice.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[str, ...]
    since: str = ""
    until: str = ""


class ModelLabIdleCostResult(BaseModel):
    """Lab-idle FRICTION cost in lane-minutes since a start, and the status-line segment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane_minutes: float
    rows: int
    unreadable: int
    since: str
    segment: str
