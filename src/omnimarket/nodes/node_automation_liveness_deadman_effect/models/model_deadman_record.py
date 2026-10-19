# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of one dead-man recording tick (OMN-20807)."""

from __future__ import annotations

from pathlib import Path

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    NonNegativeInt,
)

from omnimarket.models.liveness.model_automation_liveness import (
    ModelAutomationAlarmRecorded,
    ModelAutomationHeartbeat,
    NonEmptyStr,
)
from omnimarket.models.liveness.model_automation_liveness_deadman import (
    ModelDeadmanEpisodeKey,
    ModelDeadmanLedgerRow,
)

DEADMAN_PROCESS_ID = "watchdog/deadman"


class ModelDeadmanRecordRequest(BaseModel):
    """The rows the compute returned and what the dead-man needs to beat."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    host: NonEmptyStr
    process_started_at: AwareDatetime
    #: How many bus events this dead-man has consumed, for its heartbeat.
    consumed_offset: NonNegativeInt = 0
    rows: tuple[ModelDeadmanLedgerRow, ...] = ()
    #: Where the dead-man's recorded keys are kept; or inject a store.
    store_path: Path | None = None
    #: The ledger file and the argv that appends one row to it (the row is the
    #: last argument); or inject an appender.
    ledger_path: Path | None = None
    ledger_append_command: tuple[NonEmptyStr, ...] | None = None


class ModelDeadmanRecordResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    recorded: tuple[ModelAutomationAlarmRecorded, ...] = ()
    #: Keys found already recorded, in the store or in the ledger itself.
    skipped: tuple[ModelDeadmanEpisodeKey, ...] = ()
    #: Rows not written because an earlier append failed; order is kept.
    backlog: tuple[ModelDeadmanEpisodeKey, ...] = ()
    failure: NonEmptyStr | None = None
    publish_failures: NonNegativeInt = 0
    heartbeat: ModelAutomationHeartbeat


__all__ = [
    "DEADMAN_PROCESS_ID",
    "ModelDeadmanRecordRequest",
    "ModelDeadmanRecordResult",
]
