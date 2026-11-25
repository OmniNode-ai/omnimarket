# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, per-check records and result of node_build_delegation_checks (OMN-26621).

A build is the git sha a dev rebuild-completed event names. A check's reading on a build is pass, fail or unavailable; a check
that could not run (a lab host down, a timeout, no probe route) is unavailable and is never read as a regression (operator
ruling 2026-11-23T16:21:00Z: regressions are not tied to lab failures).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.models.delegation_check_regression import (
    ModelRegressionEvent,
    ModelSuspectMerge,
)
from omnimarket.models.lab_alarm_delivery import ModelLabAlarmDeliveryRequest


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumCheckReading(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNAVAILABLE = "unavailable"


class ModelRebuildCompletedObserved(BaseModel):
    """The fields of the deploy agent's rebuild-completed event the node reads; the rest is ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    git_sha: str = Field(min_length=1)
    runtime_lane: str = Field(min_length=1)
    status: Literal["success", "failed"] = "success"
    correlation_id: str = ""
    completed_at: AwareDatetime | None = None


class ModelCheckOutcome(_Frozen):
    """What the probe entry point answered for one check on one build."""

    reading: EnumCheckReading
    detail: str = ""
    run_id: str = ""
    finished_at: AwareDatetime | None = None
    #: The module the probe ran as (``python -m <module>``); empty when no probe ran.
    probe: str = ""


class ModelCheckRecord(_Frozen):
    """One check's row on one build: pass, fail or unavailable, the build id and the merges the build added."""

    check_id: str = Field(min_length=1)
    build_id: str = Field(min_length=1)
    previous_build_id: str = ""
    reading: EnumCheckReading
    detail: str = ""
    run_id: str = ""
    #: The module the probe ran as; empty when no probe ran (no route of ours for the check).
    probe: str = ""
    at: AwareDatetime
    #: The merges the build added over the previous recorded build, oldest first.
    merges: tuple[ModelSuspectMerge, ...] = ()


class ModelBuildRecord(_Frozen):
    """One build the node ran its checks on."""

    build_id: str = Field(min_length=1)
    runtime_lane: str = Field(min_length=1)
    previous_build_id: str = ""
    started_at: AwareDatetime
    merges: tuple[ModelSuspectMerge, ...] = ()
    checks: tuple[ModelCheckRecord, ...] = ()
    complete: bool = False


class ModelRefusedAlarm(_Frozen):
    check_id: str
    reason: str


class EnumBuildChecksStatus(StrEnum):
    RAN = "ran"
    #: The build id already has a record: a replayed rebuild-completed runs nothing.
    ALREADY_RUN = "already_run"
    #: Not a successful rebuild of the dev lane.
    SKIPPED = "skipped"
    #: The build history cannot be written under the declared state root: nothing ran, and the reason names the path.
    REFUSED = "refused"


class ModelBuildChecksResult(_Frozen):
    build_id: str
    status: EnumBuildChecksStatus
    reason: str = ""
    previous_build_id: str = ""
    records: tuple[ModelCheckRecord, ...] = ()
    regressions: tuple[ModelRegressionEvent, ...] = ()
    alarms: tuple[ModelLabAlarmDeliveryRequest, ...] = ()
    #: A regression whose alarm was refused (no owner); nothing was sent for it.
    refused: tuple[ModelRefusedAlarm, ...] = ()


__all__ = [
    "EnumBuildChecksStatus",
    "EnumCheckReading",
    "ModelBuildChecksResult",
    "ModelBuildRecord",
    "ModelCheckOutcome",
    "ModelCheckRecord",
    "ModelRebuildCompletedObserved",
    "ModelRefusedAlarm",
]
