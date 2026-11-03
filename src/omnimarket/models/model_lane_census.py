# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane census node's command, run result and schedule.

The census reads the Docker inventory of the host it runs on, so it runs from a host timer on a
host that carries a Docker Engine and nowhere else. The node's ``contract.yaml`` owns the cadence
(``schedule``). Which hosts run the census is a deployment fact: the node overlay
(``<root>/node_lane_census_effect/overlay.yaml``) supplies it and the package ships no host.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# What the collector's exit code says about the run. ``drift`` and ``drift-unpublished`` are
# census verdicts: the inventory was read. The rest name why it was not.
CensusOutcome = Literal[
    "clean",
    "drift",
    "drift-unpublished",
    "inventory-unobservable",
    "host-undeclared",
    "memory-pass",
    "collector-error",
]

OUTCOME_BY_EXIT: dict[int, CensusOutcome] = {
    0: "clean",
    30: "drift",
    8: "drift-unpublished",
    4: "inventory-unobservable",
    5: "host-undeclared",
    6: "memory-pass",
    7: "memory-pass",
    31: "memory-pass",
}


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelLaneCensusCommand(_Frozen):
    """One census run on the host that runs this process."""

    collector: NonEmptyStr
    snapshot: NonEmptyStr
    # The memory pass reads /proc and cgroup v2 files and needs a broker container named in the
    # environment, so a host opts in.
    memory: bool = False
    timeout_s: int = Field(default=900, gt=0)


class ModelLaneCensusRunResult(_Frozen):
    """What one census run observed: the collector's exit code and the snapshot it left."""

    exit_code: int
    outcome: CensusOutcome
    # The snapshot was (re)written by this run; a snapshot older than the run proves nothing.
    snapshot_fresh: bool
    # The Docker inventory was read: the collector reached its verdict and wrote the snapshot.
    docker_observed: bool
    snapshot: NonEmptyStr
    diagnostics: tuple[str, ...] = ()


class ModelLaneCensusCadence(_Frozen):
    """When the timer fires, in systemd calendar terms."""

    on_calendar: NonEmptyStr
    randomized_delay: NonEmptyStr
    persistent: bool
    description: NonEmptyStr


class ModelLaneCensusSchedule(_Frozen):
    """The contract's ``schedule`` block: one census per cadence tick on every declared host."""

    unit_name: Annotated[
        str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    ]
    collector: NonEmptyStr
    # Relative to the registry root; ONEX_DISPATCH_VENV overrides the venv.
    dispatch_venv: NonEmptyStr
    state_directory: NonEmptyStr
    snapshot_name: NonEmptyStr
    timeout_seconds: int = Field(gt=0)
    cadence: ModelLaneCensusCadence
    # The only scheduler kind. A launchd job would put the census on a Docker Desktop host that
    # is often not running, which is the failure this node replaces.
    schedulers: dict[Literal["systemd-user"], NonEmptyStr] = Field(min_length=1)


class ModelLaneCensusHostRow(_Frozen):
    """One host that runs the census."""

    name: NonEmptyStr
    scheduler: Literal["systemd-user"] = "systemd-user"
    # A Docker Engine on the host itself, not a Docker Desktop VM that a person must have started.
    docker: Literal["engine"]


class ModelLaneCensusScheduleOverlay(_Frozen):
    """The node overlay: every host that runs the census."""

    hosts: Annotated[tuple[ModelLaneCensusHostRow, ...], Field(min_length=1)]


class ModelLaneCensusScheduleResult(_Frozen):
    """The outcome of a schedule verb: a process exit code and what to print."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
