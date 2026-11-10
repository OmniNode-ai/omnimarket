# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed goal, caps, counters and stop reasons shared by every goal-driven run (OMN-21974).

A goal is an executable predicate with a hash. The caps are five numbers read from the
experiment's data file. The stop reasons are ten and typed. The first stop reached wins.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumGoalStopReason(StrEnum):
    GOAL_MET = "goal_met"
    TOKEN_CAP = "token_cap"
    WALL_CLOCK_CAP = "wall_clock_cap"
    TURN_CAP = "turn_cap"
    COMPACTION_CAP = "compaction_cap"
    NO_PROGRESS = "no_progress"
    AGENT_BLOCKED = "agent_blocked"
    OPERATOR_STOP = "operator_stop"
    INFRA_FAULT = "infra_fault"
    REFUSED_PREMISE = "refused_premise"


class EnumGoalAnalysisRole(StrEnum):
    """What a stop is in the analysis: an observation, a censoring, a failure or an exclusion."""

    OBSERVED = "observed"
    RIGHT_CENSORED = "right_censored"
    FAILURE = "failure"
    EXCLUDED = "excluded"


class EnumGoalPredicateKind(StrEnum):
    TEST_SUITE = "test_suite"
    VERIFY_RUN = "verify_run"
    LEDGER_STATE_CHECK = "ledger_state_check"


class EnumGoalRunAction(StrEnum):
    CONTINUE = "continue"
    RETRY = "retry"
    STOP = "stop"


class ModelGoal(BaseModel):
    """An executable predicate and its hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    predicate_kind: EnumGoalPredicateKind
    predicate_ref: str = Field(min_length=1)
    predicate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ModelGoalCaps(BaseModel):
    """The five caps; every one is positive and none has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    token_cap: int = Field(gt=0, strict=True)
    wall_clock_cap_s: int = Field(gt=0, strict=True)
    turn_cap: int = Field(gt=0, strict=True)
    compaction_cap: int = Field(gt=0, strict=True)
    no_progress_window_turns: int = Field(gt=0, strict=True)


class ModelGoalCounters(BaseModel):
    """The counters of a run at one instant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tokens: int = Field(ge=0)
    wall_clock_s: int = Field(ge=0)
    turns: int = Field(ge=0)
    compactions: int = Field(ge=0)
    turns_without_subcheck_change: int = Field(ge=0)
