# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the delegation readiness verdict (OMN-23181, readiness plan 2.4 'Shippable').

The verdict is PASS only when, for every declared scenario, the consecutive green UTC days reach the bar
(``required_green_days``, a contract setting), no slot failed today, and the latest DUE slot has a successful scheduled
run. Nothing here reads a clock or a file.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_delegation_scenario import (
    EnumScenarioCadence,
    ModelScenarioHistory,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumReadinessVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class ModelCadenceGrace(_Frozen):
    """How long a slot of one cadence may stay unrun before it is due: the due slot is the one before the last ``hours``."""

    cadence: EnumScenarioCadence
    hours: int = Field(ge=0)


class ModelReadinessVerdictRequest(_Frozen):
    """What the verdict is computed from: the history, the green-day bar and the due-slot grace per cadence."""

    history: ModelScenarioHistory
    required_green_days: int = Field(ge=1, le=31)
    grace: tuple[ModelCadenceGrace, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _bar_is_reachable_and_grace_is_complete(self) -> ModelReadinessVerdictRequest:
        if self.required_green_days > self.history.window_days:
            raise ValueError(
                f"required_green_days {self.required_green_days} is above window_days "
                f"{self.history.window_days}; the bar could never be reached"
            )
        declared = {entry.cadence for entry in self.grace}
        needed = {s.cadence for s in self.history.scenarios}
        if len(declared) != len(self.grace):
            raise ValueError("a cadence has two grace entries")
        if not needed <= declared:
            names = ",".join(sorted(c.value for c in needed - declared))
            raise ValueError(f"no due-slot grace declared for cadence {names}")
        return self

    def grace_hours(self, cadence: EnumScenarioCadence) -> int:
        return next(entry.hours for entry in self.grace if entry.cadence is cadence)


class ModelReadinessScenarioRow(_Frozen):
    """The verdict inputs of one scenario. ``misses`` is empty exactly when the scenario is not red."""

    scenario_id: str
    green_days: int = Field(ge=0)
    failed_today: bool
    latest_due_slot: AwareDatetime | None
    latest_due_slot_ok: bool
    misses: tuple[str, ...] = ()
    red: bool


class ModelReadinessVerdict(_Frozen):
    """PASS or FAIL for the whole suite at ``as_of``, one row per declared scenario in scenario-id order."""

    schema_id: Literal["omni.delegation-readiness-verdict/1"] = (
        "omni.delegation-readiness-verdict/1"
    )
    as_of: AwareDatetime
    required_green_days: int
    window_days: int
    verdict: EnumReadinessVerdict
    rows: tuple[ModelReadinessScenarioRow, ...]
    #: ``R3:green_days 2 of 7`` for every miss of every scenario, in scenario-id order.
    misses: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.verdict is EnumReadinessVerdict.PASS

    def by_id(self) -> dict[str, ModelReadinessScenarioRow]:
        return {row.scenario_id: row for row in self.rows}


class ModelReadinessVerdictRunCommand(_Frozen):
    """One run of the readiness verdict. It carries no value: every setting is in the node's contract."""
