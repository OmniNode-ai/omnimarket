# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model the overlay home validates <root>/node_delegation_check_regression_effect/overlay.yaml through (OMN-24562).

The overlay home pins a node's overlay to a merged revision that already holds its model, and the omnimarket
overlay check refuses a new node whose overlay has no document on the overlay home main. The model therefore lives in this shared
package and merges ahead of the node that reads it. Copied unchanged from the node's settings model on omnimarket#6700
(head 1c332fb843); no value lives here.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
    ModelFileLedgerReadRoute,
)

CheckId = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")]
Repository = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
Lane = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


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
