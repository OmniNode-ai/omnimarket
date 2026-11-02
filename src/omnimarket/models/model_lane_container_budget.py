# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed declaration, request and verdict for a lane's application container budget.

Shared by node_lane_container_budget_compute, which judges a host's container listing, and node_factory_floor_compute,
whose one-runtime line shows the verdict. The budget of each lane is data the caller supplies; no count is written in
a handler.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# UNKNOWN is a failing status: a host that cannot be read is not a host within budget. There is no warning status.
LaneBudgetStatus = Literal["PASS", "FAIL", "UNKNOWN"]
AlarmStatus = Literal["FAIL", "UNKNOWN"]


class ModelLaneContainerBudgetDeclaration(BaseModel):
    """One lane's declared budget on one host, as the private overlay lists it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    compose_project: str = Field(min_length=1)
    application_container_budget: int = Field(ge=0)
    # A running container of the lane's compose project whose name contains one of these is infrastructure (a bus, a
    # database), not an application container, and is not counted.
    infrastructure_markers: tuple[str, ...] = ()


class ModelLaneContainerBudgetRequest(BaseModel):
    """A host's recorded container listing and the budgets declared for it.

    ``docker_ps`` is the listing in the form ``docker ps -a --no-trunc --format '{{json .}}'`` prints it, one JSON
    object per line, which is the form the lane census collects. An empty listing, a line that is not a JSON object
    and a non-empty ``read_error`` all mean the host was not read.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    docker_ps: str
    read_error: str = ""
    declarations: tuple[ModelLaneContainerBudgetDeclaration, ...]


class ModelLaneContainerBudgetAlarm(BaseModel):
    """One alarm row, keyed ``lane-budget:<host>:<lane>``; a rerun rewrites the row under the same key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    host: str
    lane: str
    status: AlarmStatus
    observed: int | None
    budget: int | None
    containers: tuple[str, ...]
    detail: str


class ModelLaneContainerBudgetLane(BaseModel):
    """One lane's reading on one host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    lane: str
    status: LaneBudgetStatus
    observed: int | None
    budget: int
    containers: tuple[str, ...]
    detail: str


class ModelLaneContainerBudgetVerdict(BaseModel):
    """The host's verdict: FAIL when any lane is over budget, else UNKNOWN when any lane was not read, else PASS."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    status: LaneBudgetStatus
    failed: bool
    lanes: tuple[ModelLaneContainerBudgetLane, ...]
    alarms: tuple[ModelLaneContainerBudgetAlarm, ...]
