# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the host disk decision (OMN-23866).

The policy (every keep-days value, size cap, free-space floor and per-host exception) is a field of the request,
read by the orchestrator from ``overlays/node_host_disk_decision_compute/overlay.yaml`` and never written in code.
The decision is a pure function of the request: ``now`` is an input, and the same request always yields the same
commands and alarms, in the same order.

A command carries the ``idempotency_key`` ``<host>|<class>|<read_at>``: one key per host, class and reading, so a
redelivered command for the same reading is recognisable by the effect that executes it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from omnimarket.models.model_host_disk_reading import (
    DiskClassName,
    ModelDiskClassReading,
    ModelHostDiskVolume,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SECONDS_PER_DAY = 86400


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class HostDiskPolicyRefusedError(ValueError):
    """The policy cannot be applied to a host the request names (a host without a floor)."""


class ModelHostDiskClassPolicy(_Frozen):
    """What one class keeps. ``keep_days`` and ``cap_bytes`` are each optional; a class with neither is never evicted."""

    keep_days: int | None = Field(default=None, ge=1)
    cap_bytes: int | None = Field(default=None, ge=1)
    #: The shorter window that applies while the host is under its floor.
    pressure_keep_days: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _pressure_window_is_shorter(self) -> Self:
        if (
            self.pressure_keep_days is not None
            and self.keep_days is not None
            and self.pressure_keep_days > self.keep_days
        ):
            msg = "pressure_keep_days must not exceed keep_days"
            raise ValueError(msg)
        return self


class ModelHostDiskDefaults(_Frozen):
    """The values a host takes unless its own block names an exception."""

    floor_free_bytes: int | None = Field(default=None, ge=1)
    volume_path: NonEmptyStr = "/"
    classes: dict[DiskClassName, ModelHostDiskClassPolicy] = Field(default_factory=dict)


class ModelHostDiskHostPolicy(_Frozen):
    """One host's exceptions to the defaults; every field left out falls back to the default."""

    floor_free_bytes: int | None = Field(default=None, ge=1)
    volume_path: NonEmptyStr | None = None
    classes: dict[DiskClassName, ModelHostDiskClassPolicy] = Field(default_factory=dict)


class ModelHostDiskPolicy(_Frozen):
    """The whole policy. ``hosts`` names every host the fleet expects, so a host with no reading alarms."""

    max_reading_age_seconds: int = Field(gt=0)
    defaults: ModelHostDiskDefaults
    hosts: dict[NonEmptyStr, ModelHostDiskHostPolicy] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _every_named_host_has_a_floor(self) -> Self:
        for name in sorted(self.hosts):
            self.floor_for(name)
        return self

    def floor_for(self, host: str) -> int:
        """The host's floor, or a refusal: a policy never leaves a host without one."""
        own = self.hosts.get(host)
        if own is not None and own.floor_free_bytes is not None:
            return own.floor_free_bytes
        if self.defaults.floor_free_bytes is not None:
            return self.defaults.floor_free_bytes
        msg = f"host {host!r} has no free-space floor: set defaults.floor_free_bytes or a floor on the host"
        raise HostDiskPolicyRefusedError(msg)

    def volume_for(self, host: str) -> str:
        own = self.hosts.get(host)
        if own is not None and own.volume_path is not None:
            return own.volume_path
        return self.defaults.volume_path

    def class_policy_for(
        self, host: str, class_name: DiskClassName
    ) -> ModelHostDiskClassPolicy | None:
        own = self.hosts.get(host)
        if own is not None and class_name in own.classes:
            return own.classes[class_name]
        return self.defaults.classes.get(class_name)


class ModelHostDiskHostReading(_Frozen):
    """One host's newest reading as the projection keeps it: its volumes and its classes."""

    host_name: NonEmptyStr
    read_at: datetime
    volumes: tuple[ModelHostDiskVolume, ...]
    classes: tuple[ModelDiskClassReading, ...]

    @model_validator(mode="after")
    def _read_at_carries_a_timezone(self) -> Self:
        if self.read_at.utcoffset() is None:
            msg = "read_at must carry a timezone"
            raise ValueError(msg)
        return self


class ModelHostDiskDecisionRequest(_Frozen):
    """The decision's inputs: ``now`` (never a clock read), the policy and each host's newest reading."""

    now: datetime
    policy: ModelHostDiskPolicy
    readings: tuple[ModelHostDiskHostReading, ...] = ()

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.now.utcoffset() is None:
            msg = "now must carry a timezone"
            raise ValueError(msg)
        names = [r.host_name for r in self.readings]
        if len(set(names)) != len(names):
            dupes = sorted({n for n in names if names.count(n) > 1})
            msg = f"more than one reading for host(s): {', '.join(dupes)}"
            raise ValueError(msg)
        for name in names:
            self.policy.floor_for(name)
        return self


class EnumHostDiskReason(StrEnum):
    AGE_PAST_KEEP_DAYS = "age_past_keep_days"
    CLASS_OVER_CAP = "class_over_cap"
    HOST_UNDER_FLOOR = "host_under_floor"


#: When one command carries targets for several reasons, the command names the highest.
REASON_PRIORITY: tuple[EnumHostDiskReason, ...] = (
    EnumHostDiskReason.HOST_UNDER_FLOOR,
    EnumHostDiskReason.CLASS_OVER_CAP,
    EnumHostDiskReason.AGE_PAST_KEEP_DAYS,
)


class ModelHostDiskEvictTarget(_Frozen):
    package: str
    revision: str
    bytes: int = Field(ge=0)
    reason: EnumHostDiskReason


class ModelHostDiskCommand(_Frozen):
    """One decision: evict these entries of this class on this host.

    ``targets`` empty with ``prune_to_bytes`` set means the class has no per-entry age to choose by (a docker class)
    and the effect prunes it down to that size.
    """

    host_name: NonEmptyStr
    class_name: DiskClassName
    reason: EnumHostDiskReason
    targets: tuple[ModelHostDiskEvictTarget, ...] = ()
    prune_to_bytes: int | None = Field(default=None, ge=0)
    reading_read_at: datetime
    idempotency_key: NonEmptyStr


class EnumHostDiskAlarmKind(StrEnum):
    UNDER_FLOOR = "under_floor"
    STALE_READING = "stale_reading"
    MISSING_READING = "missing_reading"


class ModelHostDiskAlarm(_Frozen):
    host_name: NonEmptyStr
    kind: EnumHostDiskAlarmKind
    detail: NonEmptyStr
    free_bytes: int | None = None
    floor_free_bytes: int | None = None
    reading_age_seconds: int | None = None
    reading_read_at: datetime | None = None
    idempotency_key: NonEmptyStr


class ModelHostDiskDecision(_Frozen):
    decided_at: datetime
    commands: tuple[ModelHostDiskCommand, ...] = ()
    alarms: tuple[ModelHostDiskAlarm, ...] = ()


__all__: list[str] = [
    "REASON_PRIORITY",
    "SECONDS_PER_DAY",
    "EnumHostDiskAlarmKind",
    "EnumHostDiskReason",
    "HostDiskPolicyRefusedError",
    "ModelHostDiskAlarm",
    "ModelHostDiskClassPolicy",
    "ModelHostDiskCommand",
    "ModelHostDiskDecision",
    "ModelHostDiskDecisionRequest",
    "ModelHostDiskDefaults",
    "ModelHostDiskEvictTarget",
    "ModelHostDiskHostPolicy",
    "ModelHostDiskHostReading",
    "ModelHostDiskPolicy",
]
