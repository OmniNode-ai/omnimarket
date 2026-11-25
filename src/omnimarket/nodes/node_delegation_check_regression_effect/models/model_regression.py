# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed settings, readings, events and results of the delegation check regression alarm.

Nothing here names a check, a repository, a workflow or an owner: the node's overlay declares them and the contract's
topics block declares the bus surface.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.lab_alarm_delivery import ModelLabAlarmDeliveryRequest
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
    ModelFileLedgerReadRoute,
)

CheckId = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")]
Repository = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
Lane = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumRunVerdict(StrEnum):
    """A finished probe run: a success is a pass, a failure is a fail; any other run is not a reading."""

    PASS = "pass"
    FAIL = "fail"


class EnumChangeKind(StrEnum):
    MERGE = "merge"
    DEV_BUILD = "dev_build"
    HOST_CHANGE = "host_change"
    MODEL_SETTING = "model_setting"


class ModelWatchedCheck(_Frozen):
    """One delegation check: the scheduled workflow whose finished runs are its results."""

    check_id: CheckId
    repository: Repository
    workflow: str = Field(min_length=1)
    events: tuple[str, ...] = ("schedule",)
    #: The probe's schedule interval; a red followed by a green inside one interval (plus the grace) is a flap.
    interval_seconds: int = Field(gt=0)
    #: The owner a regression of this check is raised to; the settings' default owner when absent.
    owner: str | None = None


class ModelChangeMarker(_Frozen):
    """A ledger row that records a host or model setting change: a row of this type naming every listed term."""

    kind: EnumChangeKind
    row_type: str = Field(min_length=1)
    contains_all: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _setting_kind(self) -> ModelChangeMarker:
        if self.kind not in (EnumChangeKind.HOST_CHANGE, EnumChangeKind.MODEL_SETTING):
            raise ValueError("a ledger marker records a host or model setting change")
        return self


class ModelMergeSource(_Frozen):
    """A repository whose merges to a branch are changes between two probe runs."""

    repository: Repository
    #: The directory of the repository's canonical clone under the registry root.
    clone: str = Field(min_length=1)
    branch: str = Field(min_length=1)


class ModelRegressionSettings(_Frozen):
    """The overlay: watched checks, owners, thresholds and where changes are read from."""

    #: The lane the alarm is raised to (the orchestrator) and the owner named on every alarm and fix-lane request.
    alarm_lane: Lane
    owner: str = Field(min_length=1)
    regression_condition: str = Field(
        default="delegation_check_regression", min_length=1
    )
    flap_condition: str = Field(default="delegation_check_flap", min_length=1)
    #: An alarm must reach the lab alarm path inside this many seconds of the red run being readable.
    max_alarm_latency_seconds: int = Field(default=900, gt=0)
    #: The scan cadence; it must fit inside the alarm latency bound.
    scan_interval_seconds: int = Field(default=300, gt=0)
    flap_window_seconds: int = Field(default=86400, gt=0)
    #: The count of flaps inside the window that raises a flap alarm (the second flap).
    flap_alarm_count: int = Field(default=2, ge=1)
    flap_grace_seconds: int = Field(default=600, ge=0)
    #: A transition whose first red run is older than this is history, not a regression to raise.
    max_transition_age_seconds: int = Field(default=86400, gt=0)
    runs_per_check: int = Field(default=20, ge=2, le=100)
    gateway_timeout_seconds: float = Field(default=60.0, gt=0)
    checks: tuple[ModelWatchedCheck, ...] = Field(min_length=1)
    change_markers: tuple[ModelChangeMarker, ...] = ()
    merge_sources: tuple[ModelMergeSource, ...] = ()
    #: How ledger rows are read (host and model setting changes, live CLAIMs): the file the variable names or the read command.
    ledger_read: Annotated[
        ModelFileLedgerReadRoute | ModelCommandLedgerReadRoute,
        Field(discriminator="route"),
    ]
    bus_lane: Lane = "dev"
    #: The registry root that holds the clones, the ledger and the state directory. The runtime builds the node's ports from
    #: it; a command-line run names it with ``--registry-root`` instead.
    registry_root: Path | None = None
    #: The runtime lane whose rebuilds are the dev builds between two probe runs.
    dev_runtime_lane: str = Field(default="dev", min_length=1)
    dev_builds_relpath: str = Field(
        default=".onex_state/delegation-check-regression/dev-builds.jsonl", min_length=1
    )
    #: The file holding the transitions already raised, relative to the registry root.
    seen_relpath: str = Field(
        default=".onex_state/delegation-check-regression/seen.json", min_length=1
    )

    @model_validator(mode="after")
    def _consistent(self) -> ModelRegressionSettings:
        if self.scan_interval_seconds > self.max_alarm_latency_seconds:
            raise ValueError(
                "the scan interval must fit inside the alarm latency bound"
            )
        ids = [c.check_id for c in self.checks]
        if len(set(ids)) != len(ids):
            raise ValueError("a check is watched once")
        return self


class ModelProbeRun(_Frozen):
    """One finished run of a check's workflow."""

    run_id: str = Field(min_length=1)
    at: AwareDatetime
    verdict: EnumRunVerdict
    ref: str = ""


class ModelChange(_Frozen):
    """Something that changed between two probe runs."""

    kind: EnumChangeKind
    at: AwareDatetime
    ref: str = ""
    summary: str = Field(min_length=1)


class ModelRunRef(_Frozen):
    run_id: str
    at: AwareDatetime
    ref: str = ""


class ModelRegressionEvent(_Frozen):
    """One green to red transition of one check, with what changed between the last green run and the first red run."""

    event_id: str
    check_id: CheckId
    repository: Repository
    workflow: str
    last_green: ModelRunRef
    first_red: ModelRunRef
    changes: tuple[ModelChange, ...]
    owner: str
    alarm_lane: Lane
    detected_at: AwareDatetime
    #: Seconds from the red run to the detection that raised the alarm.
    alarm_latency_seconds: float
    #: True when the alarm was raised later than the latency bound allows (a late read or a replay).
    late: bool
    #: The next green run, when the check had already recovered at detection.
    recovered_at: AwareDatetime | None = None


class ModelFlap(_Frozen):
    """A red run that the next run, inside one interval, turned green again."""

    check_id: CheckId
    red: ModelRunRef
    green: ModelRunRef


class ModelFlapCount(_Frozen):
    check_id: CheckId
    flaps: tuple[ModelFlap, ...]

    @property
    def count(self) -> int:
        return len(self.flaps)


class ModelFixLaneRequest(_Frozen):
    """One fix lane for one regressed check."""

    request_id: str
    check_id: CheckId
    event_id: str
    owner: str
    alarm_lane: Lane
    first_red: ModelRunRef


class ModelHeldFix(_Frozen):
    check_id: CheckId
    event_id: str
    held_by: str


class ModelUnreadCheck(_Frozen):
    check_id: CheckId
    reason: str


class ModelRegressionScanRequest(_Frozen):
    """One scan. ``now`` is the detection time (the replay clock in a replay)."""

    now: AwareDatetime
    #: False reads and decides and writes nothing: no event, no alarm, no fix lane, no seen record.
    write: bool = True
    check_ids: tuple[CheckId, ...] = ()


class ModelRegressionScanResult(_Frozen):
    now: datetime
    events: tuple[ModelRegressionEvent, ...] = ()
    flap_counts: tuple[ModelFlapCount, ...] = ()
    alarms: tuple[ModelLabAlarmDeliveryRequest, ...] = ()
    fix_lanes: tuple[ModelFixLaneRequest, ...] = ()
    held_fixes: tuple[ModelHeldFix, ...] = ()
    unread: tuple[ModelUnreadCheck, ...] = ()
    skipped_seen: tuple[str, ...] = ()
    #: A publish that failed; the transition is not recorded as raised, so the next scan retries it.
    publish_failures: tuple[str, ...] = ()


class ModelRebuildCompletedObserved(BaseModel):
    """The fields of the deploy agent's rebuild-completed event that a dev build record keeps; the rest is ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    git_sha: str = Field(min_length=1)
    completed_at: AwareDatetime
    runtime_lane: str = Field(min_length=1)
    requested_git_ref: str = ""


class ModelDevBuildRecorded(_Frozen):
    git_sha: str
    completed_at: AwareDatetime
    runtime_lane: str
    recorded: bool
