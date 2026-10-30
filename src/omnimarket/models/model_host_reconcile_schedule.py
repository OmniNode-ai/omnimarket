# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host reconcile node's schedule: the contract's block, the host facts overlay and a result.

The node's ``contract.yaml`` owns the cadence (``schedule``). Which hosts run the reconciler,
and what schedules it on each, is a deployment fact: the node's overlay
(``<root>/node_host_reconcile_effect/overlay.yaml``) supplies it and the package ships no host.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# What runs the reconciler on a host. Only ``systemd-user`` is installed by the node; the others
# are declared by a host whose scheduler another owner installs, and must name it.
SchedulerKind = Literal["systemd-user", "launchd", "external"]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelHostReconcileCadence(_Frozen):
    """When the timer fires, in systemd calendar terms."""

    on_calendar: NonEmptyStr
    randomized_delay: NonEmptyStr
    persistent: bool
    description: NonEmptyStr


class ModelHostReconcileSchedule(_Frozen):
    """The contract's ``schedule`` block: one repair-mode run per cadence tick, on every host."""

    unit_name: Annotated[
        str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    ]
    mode: Literal["repair"]
    branch: NonEmptyStr
    timeout_seconds: int = Field(gt=0)
    # Both are relative to the registry root; ONEX_DISPATCH_VENV overrides the venv.
    dispatch_venv: NonEmptyStr
    state_directory: NonEmptyStr
    cadence: ModelHostReconcileCadence
    # Each scheduler kind a host may declare, with who installs it.
    schedulers: dict[SchedulerKind, NonEmptyStr] = Field(min_length=1)


class ModelHostReconcileHostRow(_Frozen):
    """One host that runs the reconciler, and what schedules it there."""

    name: NonEmptyStr
    # ``None`` is a host that declares the reconciler and nothing that runs it: a gap.
    scheduler: SchedulerKind | None = None
    # The unit or label that runs the reconciler on a host whose scheduler is not installed by
    # this node (``launchd``, ``external``).
    scheduled_by: NonEmptyStr | None = None


class ModelHostReconcileScheduleOverlay(_Frozen):
    """The node overlay: every host that runs the reconciler."""

    hosts: Annotated[tuple[ModelHostReconcileHostRow, ...], Field(min_length=1)]


class ModelHostReconcileScheduleResult(_Frozen):
    """The outcome of a schedule verb: a process exit code and what to print."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
