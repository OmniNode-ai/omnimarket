# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed step event, state record and reduce command of a host unit install (OMN-23957).

One install of one unit on one host is one record, keyed by ``(host, unit)``. The orchestrator publishes one
``ModelHostInstallStepEvent`` per step it takes; the reducer folds each into the record and the record is the projection
every reader uses. Digests are lowercase hex; an empty digest means the step has none to give.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumHostInstallStep(StrEnum):
    REQUEST = "request"
    SNAPSHOT = "snapshot"
    INSTALL = "install"
    VERIFY = "verify"
    RUN = "run"
    ROLLBACK = "rollback"


class EnumHostInstallOutcome(StrEnum):
    STARTED = "started"
    OK = "ok"
    FAILED = "failed"


class EnumHostInstallState(StrEnum):
    REQUESTED = "requested"
    SNAPSHOTTED = "snapshotted"
    INSTALLING = "installing"
    INSTALLED = "installed"
    VERIFIED = "verified"
    RUN_OK = "run_ok"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class ModelHostInstallStepEvent(BaseModel):
    """One step the install workflow took, as published on the step topic.

    ``render_digest`` is the digest of the unit file the declared render produces (carried by the request step).
    ``rollback_target_digest`` is the digest of the file the snapshot step found installed (empty when none was).
    ``installed_digest`` is the digest of the file read back after the install step, after the verify read, or after a
    rollback.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    host: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    step: EnumHostInstallStep
    outcome: EnumHostInstallOutcome
    actor: str = Field(min_length=1)
    at: datetime
    render_digest: str = Field(default="", pattern=r"^[0-9a-f]*$")
    installed_digest: str = Field(default="", pattern=r"^[0-9a-f]*$")
    rollback_target_digest: str = Field(default="", pattern=r"^[0-9a-f]*$")
    detail: str = ""


class ModelHostInstallHistoryEntry(BaseModel):
    """One folded step, kept in the record so a reader sees who did what and when."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step: EnumHostInstallStep
    outcome: EnumHostInstallOutcome
    state_after: EnumHostInstallState
    actor: str
    at: datetime
    detail: str = ""


class ModelHostInstallRecord(BaseModel):
    """The projection: the state and digests of one unit install on one host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    host: str
    unit: str
    state: EnumHostInstallState
    render_digest: str
    installed_digest: str = ""
    rollback_target_digest: str = ""
    requested_by: str
    last_actor: str
    requested_at: datetime
    updated_at: datetime
    rollback_failures: int = 0
    failure_detail: str = ""
    history: tuple[ModelHostInstallHistoryEntry, ...] = ()


class ModelHostInstallReduceCommand(BaseModel):
    """The prior record of the unit on the host (absent for the first request) and the step to fold into it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prior: ModelHostInstallRecord | None = None
    event: ModelHostInstallStepEvent


__all__: list[str] = [
    "EnumHostInstallOutcome",
    "EnumHostInstallState",
    "EnumHostInstallStep",
    "ModelHostInstallHistoryEntry",
    "ModelHostInstallRecord",
    "ModelHostInstallReduceCommand",
    "ModelHostInstallStepEvent",
]
