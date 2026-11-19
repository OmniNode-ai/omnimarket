# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed facts, policy and outcomes of a default-branch alarm scan."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from omnimarket.models.model_runtime_tick_schedule import ModelScheduledFire

Lane = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")]
Owner = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@/-]*$")]
Repository = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
Branch = Literal["main", "dev"]
Commit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
CheckName = Annotated[str, Field(min_length=1, pattern=r"^[^|\r\n]+$")]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumAlarmFailure(StrEnum):
    CONFIGURATION_UNREADABLE = "configuration_unreadable"
    INVENTORY_UNREADABLE = "inventory_unreadable"
    CHECK_RUNS_UNREADABLE = "check_runs_unreadable"
    CLAIMS_UNREADABLE = "claims_unreadable"
    LEDGER_UNREADABLE = "ledger_unreadable"
    APPEND_FAILED = "append_failed"


class ModelAlarmSettings(_Frozen):
    parent_lane: Lane
    host: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    bus_lane: Lane = "dev"
    scm_owner: str = Field(default="", pattern=r"^[A-Za-z0-9_.-]*$")
    # A drill names the repositories to scan, and its rows carry drill=yes.
    drill_repos: tuple[Repository, ...] = ()


class ModelAlarmTarget(_Frozen):
    repo: Repository
    branch: Branch
    head_sha: Commit
    required_checks: tuple[CheckName, ...] = Field(min_length=1)
    owner: Owner | None = None
    related_prs: tuple[str, ...] = ()
    related_tickets: tuple[str, ...] = ()


class ModelAlarmFailure(_Frozen):
    kind: EnumAlarmFailure
    detail: str
    repo: Repository | None = None


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
    owner_source: Literal["claim", "registry", "none"]


class ModelRedMainAlarmResult(_Frozen):
    fire_id: str
    alarms: tuple[ModelRedMainAlarm, ...] = ()
    failures: tuple[ModelAlarmFailure, ...] = ()


class ModelThroughputDecidedRecord(BaseModel):
    """The shared topic carries decision results as well as schedule fires."""

    model_config = ConfigDict(frozen=True, extra="allow")

    def as_fire(self) -> ModelScheduledFire | None:
        try:
            return ModelScheduledFire.model_validate(self.model_dump())
        except ValidationError:
            return None
