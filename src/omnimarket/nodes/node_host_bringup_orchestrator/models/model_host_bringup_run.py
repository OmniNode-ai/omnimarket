# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models for the host bring-up run (OMN-21899).

The run loops probe, plan, one action, re-probe over the declared target graph,
under a per-host lease with a fence, and ends with exactly one terminal per run
id. The timing values are typed fields set by the overlay, never constants; the
request refuses a ``lease_grace_s`` that does not exceed ``action_timeout_s``
(condition 4 of the OMN-21882 design-review verdict).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.host_bringup import EnumBringupAction
from omnimarket.nodes.node_host_bringup_plan_compute.models.model_host_bringup_plan import (
    EnumObservedState,
    ModelBringupGraph,
)

_TARGET_NAME = r"^[a-z][a-z0-9]*([_-][a-z0-9]+)*$"
_HOST = r"^[a-z][a-z0-9-]*$"


class EnumRunOutcome(StrEnum):
    """How a run ended. Each outcome maps to one exit code of plan section 4.4."""

    COMPLETED = "completed"
    FAILED = "failed"
    WAITING = "waiting"
    USAGE = "usage"
    LEASE_HELD = "lease_held"
    REFUSED_STALE = "refused_stale"
    ABANDONED = "abandoned"


EXIT_CODE: dict[EnumRunOutcome, int] = {
    EnumRunOutcome.COMPLETED: 0,
    EnumRunOutcome.FAILED: 1,
    EnumRunOutcome.USAGE: 2,
    EnumRunOutcome.LEASE_HELD: 3,
    EnumRunOutcome.REFUSED_STALE: 3,
    EnumRunOutcome.WAITING: 75,
}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelHostBringupRunRequest(_Frozen):
    """One run of the graph toward one top target on one host."""

    host: str = Field(pattern=_HOST)
    run_id: str = Field(min_length=1)
    graph: ModelBringupGraph
    top_target: str = Field(pattern=_TARGET_NAME)
    lease_grace_s: int = Field(gt=0)
    action_timeout_s: int = Field(gt=0)
    wait_timeout_s: int = Field(ge=0)
    poll_interval_s: int = Field(gt=0)
    max_passes: int = Field(gt=0)
    no_wait: bool = False

    @model_validator(mode="after")
    def _grace_exceeds_action_timeout(self) -> Self:
        if self.lease_grace_s <= self.action_timeout_s:
            raise ValueError(
                f"lease_grace_s ({self.lease_grace_s}) must exceed action_timeout_s "
                f"({self.action_timeout_s}), or a successor can take over while an "
                "action is still in flight"
            )
        return self


class ModelLeaseGrant(_Frozen):
    """The lease as a run took it: its fence, the previous holder and the owed set."""

    fence: int = Field(ge=1)
    owed: tuple[str, ...] = ()


class ModelActionResult(_Frozen):
    """What one action reported. ``ok`` false stops the run."""

    ok: bool
    detail: str = ""


class ModelBringupStepEvent(_Frozen):
    """One target transition of one run, emitted on the bus."""

    run_id: str
    host: str
    fence: int
    target: str
    action: EnumBringupAction
    before: EnumObservedState
    after: EnumObservedState
    at: int


class ModelBringupTerminalEvent(_Frozen):
    """The one terminal of a run id."""

    run_id: str
    host: str
    fence: int
    outcome: EnumRunOutcome
    detail: str = ""
    at: int


class ModelHostBringupRunResult(_Frozen):
    """The receipt of one run."""

    run_id: str
    host: str
    fence: int
    outcome: EnumRunOutcome
    exit_code: int
    steps: tuple[ModelBringupStepEvent, ...] = ()
    abandoned_run_ids: tuple[str, ...] = ()
    failed_target: str | None = None
    person_line: str | None = None
    remaining: tuple[str, ...] = ()
    detail: str = ""


__all__ = [
    "EXIT_CODE",
    "EnumRunOutcome",
    "ModelActionResult",
    "ModelBringupStepEvent",
    "ModelBringupTerminalEvent",
    "ModelHostBringupRunRequest",
    "ModelHostBringupRunResult",
    "ModelLeaseGrant",
]
