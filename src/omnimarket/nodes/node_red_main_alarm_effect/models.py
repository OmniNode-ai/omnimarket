# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed facts, policy and outcomes of a default-branch alarm scan."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Lane = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")]
Owner = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@/-]*$")]
Repository = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
Branch = Literal["main", "dev"]
Commit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
CheckName = Annotated[str, Field(min_length=1, pattern=r"^[^|\r\n]+$")]
ConfigRoot = Annotated[str, Field(pattern=r"^/[A-Za-z0-9._/-]*$")]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumAlarmFailure(StrEnum):
    CONFIGURATION_UNREADABLE = "configuration_unreadable"
    INVENTORY_UNREADABLE = "inventory_unreadable"
    CHECK_RUNS_UNREADABLE = "check_runs_unreadable"
    CLAIMS_UNREADABLE = "claims_unreadable"
    LEDGER_UNREADABLE = "ledger_unreadable"
    APPEND_FAILED = "append_failed"


class ModelAlarmRepository(_Frozen):
    """One default branch the alarm reads, with the owner the repository map declares.

    The branch's head is read from the SCM gateway (the newest push run on the branch) and
    its required contexts are these, so the scan needs no PR watcher snapshot, which is
    written on the operator's laptop only.
    """

    repo: Repository
    branch: Branch
    owner: Owner
    required_checks: tuple[CheckName, ...] = Field(min_length=1)


class ModelAlarmSettings(_Frozen):
    parent_lane: Lane
    host: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    bus_lane: Lane = "dev"
    scm_owner: str = Field(default="", pattern=r"^[A-Za-z0-9_.-]*$")
    # A drill names the repositories to scan, and its rows carry drill=yes.
    drill_repos: tuple[Repository, ...] = ()
    # The declared owner map. When it names repositories the scan reads exactly these, through
    # the SCM gateway; when it is empty the scan reads the PR watcher snapshot, as before.
    repositories: tuple[ModelAlarmRepository, ...] = ()
    # The directory holding config/lab/ci_bus_lanes.yaml in the runtime that runs the node. A runtime
    # that mounts it but exports no ONEX_WORKSPACE_CONFIG_ROOT cannot resolve the bus lane, and the node's
    # scan then answers a configuration failure every fire. The overlay declares the mount; a root the
    # environment already names wins.
    workspace_config_root: ConfigRoot | None = None


class ModelAlarmTarget(_Frozen):
    repo: Repository
    branch: Branch
    head_sha: Commit
    required_checks: tuple[CheckName, ...] = Field(min_length=1)
    owner: Owner | None = None
    # The owner the node's overlay map declares for this repository: used after a live claim and
    # the repository registry's owner, and before the parent lane.
    declared_owner: Owner | None = None
    related_prs: tuple[str, ...] = ()
    related_tickets: tuple[str, ...] = ()


class ModelAlarmFailure(_Frozen):
    kind: EnumAlarmFailure
    detail: str
    repo: Repository | None = None
    branch: Branch | None = None


class ModelAlarmInventory(_Frozen):
    targets: tuple[ModelAlarmTarget, ...] = ()
    failures: tuple[ModelAlarmFailure, ...] = ()


class ModelRedMainAlarm(_Frozen):
    repo: Repository
    branch: Branch
    head_sha: Commit
    check_name: CheckName
    conclusion: str
    first_red_at: Annotated[datetime, AwareDatetime]
    owner: Owner
    owner_source: Literal["claim", "registry", "declared", "none"]


class ModelUnreadableAlarm(_Frozen):
    """A default branch the scan could not read, raised as an alarm row with an owner."""

    repo: Repository
    branch: Branch
    failure: EnumAlarmFailure
    first_seen_at: Annotated[datetime, AwareDatetime]
    owner: Owner
    owner_source: Literal["declared", "parent"]


class ModelRedMainAlarmResult(_Frozen):
    fire_id: str
    alarms: tuple[ModelRedMainAlarm, ...] = ()
    unreadable_alarms: tuple[ModelUnreadableAlarm, ...] = ()
    failures: tuple[ModelAlarmFailure, ...] = ()
