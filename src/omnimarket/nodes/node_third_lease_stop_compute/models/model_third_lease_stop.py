# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Types of the third-lease stop: the row grammar, the configuration, the request and the decisions.

Every value is data. The row grammar and the replay window come from the node's
contract; the lease count that stops, the mode and the row source come from the
``third_lease_stop`` overlay. The handler holds none of them.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumThirdLeaseStopMode(StrEnum):
    """``report-only`` emits a would-stop record and never blocks; ``enforce`` refuses the lease."""

    REPORT_ONLY = "report-only"
    ENFORCE = "enforce"


class EnumThirdLeaseStopDecision(StrEnum):
    STOP = "stop"
    ADMIT = "admit"


class ModelLeaseRowGrammar(_Frozen):
    """Which ledger rows are cause leases and which are go-and-see records."""

    lease_row_type: str = Field(min_length=1)
    cause_cell: str = Field(min_length=1)
    lane_cell: str = Field(min_length=1)
    # One lease is written as a pair of rows by the landing controller (the controller's own lane and
    # its worker); the prefixes are stripped so both rows name the same lease.
    lease_lane_prefixes: tuple[str, ...] = ()
    record_row_type: str = Field(min_length=1)
    record_cell: str = Field(min_length=1)


class ModelThirdLeaseStopConfig(_Frozen):
    """The grammar and the replay window of the contract plus the overlay's three keys."""

    stop_class_id: str = Field(min_length=1)
    grammar: ModelLeaseRowGrammar
    replay_window_hours: int = Field(gt=0)
    lease_count: int = Field(ge=2)
    mode: EnumThirdLeaseStopMode
    row_source: str = Field(min_length=1)


class ModelThirdLeaseStopRequest(_Frozen):
    """Ledger rows as text, the clock, the configuration and, optionally, the one cause asking for a lease."""

    rows: tuple[str, ...]
    now: AwareDatetime
    config: ModelThirdLeaseStopConfig
    cause: str | None = None


class ModelCauseDecision(_Frozen):
    """The gate for one cause: would the next lease be admitted."""

    cause: str
    lease_count: int = Field(ge=0)
    decision: EnumThirdLeaseStopDecision
    enforced: bool
    last_lease_at: datetime | None = None
    go_and_see_at: datetime | None = None
    reason: str


class ModelWouldStop(_Frozen):
    """One lease that the stop would have refused: the nth lease of a cause with no record after the lease before it."""

    cause: str
    lease_number: int = Field(ge=1)
    lease_id: str
    lease_at: datetime
    previous_lease_at: datetime
    mode: EnumThirdLeaseStopMode
    stop_class_id: str


class ModelThirdLeaseStopResult(_Frozen):
    mode: EnumThirdLeaseStopMode
    lease_count_threshold: int
    decisions: tuple[ModelCauseDecision, ...]
    # Leases inside the replay window that the stop would have refused. Empty when the cause filter
    # selects a cause with none.
    would_stop: tuple[ModelWouldStop, ...]
    blocks: bool
    causes_seen: int
    leases_seen: int
    records_seen: int
    rows_unreadable: int
    line: str
