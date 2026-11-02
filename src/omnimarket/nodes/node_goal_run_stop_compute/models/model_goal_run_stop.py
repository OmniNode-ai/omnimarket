# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, decision and rules of the goal-run stop decision (OMN-21974)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_goal_run import (
    EnumGoalAnalysisRole,
    EnumGoalRunAction,
    EnumGoalStopReason,
    ModelGoal,
    ModelGoalCaps,
    ModelGoalCounters,
)


class ModelGoalRunSignals(BaseModel):
    """What the caller observed that is not a counter. `detail` is the reason text it carries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    goal_met: bool = False
    agent_blocked: bool = False
    operator_stop: bool = False
    refused_premise: bool = False
    infra_fault: bool = False
    detail: str = ""


class ModelLoopStatus(BaseModel):
    """A stop state a loop returned; the contract maps it to a reason, or to no stop."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    loop: str = Field(min_length=1)
    status: str = Field(min_length=1)


class ModelGoalStopRecord(BaseModel):
    """The first stop of a run, as recorded. A later decision for the same run returns it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stop_reason: EnumGoalStopReason
    counters_at_stop: ModelGoalCounters
    detail: str = ""


class ModelGoalRunStopRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    goal: ModelGoal
    caps_file_text: str
    counters: ModelGoalCounters
    signals: ModelGoalRunSignals = ModelGoalRunSignals()
    loop_status: ModelLoopStatus | None = None
    infra_fault_retries_used: int = Field(default=0, ge=0)
    prior_stop: ModelGoalStopRecord | None = None


class ModelGoalRunAnalysis(BaseModel):
    """The fixed analysis role of a stop reason, with the flags the analysis reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: EnumGoalAnalysisRole
    right_censored: bool
    counts_as_success: bool
    in_denominator: bool
    #: A failure stop is also reported read as censored (the sensitivity reading).
    alternate_role: EnumGoalAnalysisRole | None = None
    #: The time and tokens a censored run carries: the caps, never the counters.
    censored_at: ModelGoalCounters | None = None
    exclusion_reason: str = ""
    #: An excluded stop is counted by its reason, never dropped silently.
    counted: bool = False
    scored_as_giving_up: bool = False


class ModelGoalRunStopDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    run_key: str
    caps_file_sha256: str
    caps: ModelGoalCaps
    action: EnumGoalRunAction
    stop_reason: EnumGoalStopReason | None = None
    retry_reason: EnumGoalStopReason | None = None
    reached_reasons: tuple[EnumGoalStopReason, ...] = ()
    counters_at_stop: ModelGoalCounters
    analysis: ModelGoalRunAnalysis | None = None
    detail: str = ""

    def record(self) -> ModelGoalStopRecord:
        """The record a later decision of this run is given as `prior_stop`."""
        if self.stop_reason is None:
            raise ValueError("a decision that did not stop has no record")
        return ModelGoalStopRecord(
            stop_reason=self.stop_reason,
            counters_at_stop=self.counters_at_stop,
            detail=self.detail,
        )


class ModelStopRole(BaseModel):
    """The contract's analysis role of one reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: EnumGoalAnalysisRole
    counts_as_success: bool = False
    alternate_role: EnumGoalAnalysisRole | None = None
    exclusion_reason: str = ""


class ModelGoalRunStopRules(BaseModel):
    """The `stop_rules` block of contract.yaml. Change a value there and the decision changes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    infra_fault_retry_limit: int = Field(ge=0)
    #: Order in which reasons reached at one instant resolve; the first listed wins.
    precedence: tuple[EnumGoalStopReason, ...]
    stop_roles: dict[EnumGoalStopReason, ModelStopRole]
    #: loop name -> loop status value -> reason, or null for a state that is not a stop.
    loop_status_map: dict[str, dict[str, EnumGoalStopReason | None]]

    @model_validator(mode="after")
    def _every_reason_has_a_role_and_a_place(self) -> ModelGoalRunStopRules:
        every = set(EnumGoalStopReason)
        if set(self.stop_roles) != every:
            raise ValueError("stop_roles must name each of the ten reasons")
        if set(self.precedence) != every or len(self.precedence) != len(every):
            raise ValueError("precedence must list each of the ten reasons once")
        return self
