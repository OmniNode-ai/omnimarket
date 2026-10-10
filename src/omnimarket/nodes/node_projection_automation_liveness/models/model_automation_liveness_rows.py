# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The durable rows the automation-liveness projection keeps.

Three relations, one model each:

* ``automation_liveness_state``: one row per declared process and host,
  present from the moment the overlay declares it, whether or not it has ever
  emitted.
* ``automation_run_history``: append-only, one row per run phase event.
* ``automation_alarm_episodes``: one row per alarm episode with its delivery
  and recording receipts.

The synthetic ``*_key`` columns exist so every exposure has a cursor that is
unique per row; they are derived from the natural key and never stored apart
from it.
"""

from __future__ import annotations

from uuid import UUID

from omnibase_core.enums.enum_liveness_state import EnumLivenessState
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, NonNegativeInt

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAlarmDeliveryRoute,
    EnumAlarmSeverity,
    EnumAutomationEmitter,
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    EnumAutomationProcessState,
    EnumAutomationRunOutcome,
    EnumAutomationRunPhase,
    NonEmptyStr,
    ProcessId,
    Sha256Hex,
)


class ModelAutomationLivenessStateRow(BaseModel):
    """What the projection knows about one process on one host.

    A declared process that never emitted has its identity, ``declared_at``,
    ``process_state`` and ``contract_digest`` and nothing else: every run,
    heartbeat and verdict field is absent, and both counters are zero.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    process_id: ProcessId
    host: NonEmptyStr
    declared_at: AwareDatetime | None = Field(
        default=None,
        description="When the overlay that declares this process was loaded.",
    )
    process_state: EnumAutomationProcessState | None = Field(
        default=None, description="The overlay entry's lifecycle state."
    )
    contract_digest: Sha256Hex | None = None
    last_run_at: AwareDatetime | None = Field(
        default=None, description="The newest start or finish of a run."
    )
    last_outcome: EnumAutomationRunOutcome | None = None
    last_work_at: AwareDatetime | None = Field(
        default=None, description="When a run last did work."
    )
    last_did_work_count: NonNegativeInt | None = None
    last_demand_count: NonNegativeInt | None = None
    failures_in_window: NonNegativeInt = 0
    consecutive_idle_with_demand: NonNegativeInt = 0
    last_heartbeat_at: AwareDatetime | None = None
    last_progress_at: AwareDatetime | None = None
    progress_counter: NonNegativeInt | None = None
    open_run_started_at: AwareDatetime | None = Field(
        default=None, description="Start of the oldest run with no finish."
    )
    verdict: EnumAutomationLivenessVerdict | None = None
    verdict_reason: EnumAutomationLivenessReason | None = None
    verdict_state: EnumLivenessState | None = None
    verdict_since: AwareDatetime | None = None
    verdict_evaluated_at: AwareDatetime | None = None
    open_episode_id: UUID | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.process_id, self.host)

    @property
    def process_key(self) -> str:
        """The row's unique cursor: process and host together."""
        return state_key_text(self.process_id, self.host)


class ModelAutomationRunRow(BaseModel):
    """One run phase event, kept as observed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process_id: ProcessId
    host: NonEmptyStr
    run_id: NonEmptyStr
    phase: EnumAutomationRunPhase
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    outcome: EnumAutomationRunOutcome | None = None
    exit_code: int | None = None
    did_work_count: NonNegativeInt | None = None
    demand_count: NonNegativeInt | None = None
    unseen_runs: NonNegativeInt = 0
    work_unit: NonEmptyStr | None = None
    evidence_ref: NonEmptyStr
    emitter: EnumAutomationEmitter
    observed_at: AwareDatetime
    contract_digest: Sha256Hex

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.process_id, self.host, self.run_id, self.phase.value)

    @property
    def run_key(self) -> str:
        """The row's unique cursor: the natural key as one string."""
        return "#".join(self.key)


class ModelAutomationAlarmEpisodeRow(BaseModel):
    """One alarm episode and the receipts that followed it.

    The raise, the delivery attempts and the ledger record come from different
    components on different topics, so any of them can be folded first. Every
    field the raise supplies is therefore optional: an episode first seen from
    a delivery or a record is a stub that the raise later completes. Delivery
    and recording keep order-independent summaries (earliest success, newest
    attempt) rather than counters, so a replayed receipt changes nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    episode_id: UUID
    process_id: ProcessId | None = None
    host: NonEmptyStr | None = None
    verdict: EnumAutomationLivenessVerdict | None = None
    state: EnumLivenessState | None = None
    reason: EnumAutomationLivenessReason | None = None
    severity: EnumAlarmSeverity | None = None
    opened_at: AwareDatetime | None = None
    delivery_due_at: AwareDatetime | None = None
    evidence_ref: NonEmptyStr | None = None
    action: NonEmptyStr | None = None
    cleared_at: AwareDatetime | None = None
    last_attempt_at: AwareDatetime | None = None
    last_attempt_route: EnumAlarmDeliveryRoute | None = None
    last_attempt_delivered: bool | None = None
    last_attempt_failure: NonEmptyStr | None = None
    delivered_at: AwareDatetime | None = Field(
        default=None, description="The earliest successful delivery attempt."
    )
    delivery_route: EnumAlarmDeliveryRoute | None = None
    delivery_ref: NonEmptyStr | None = None
    recorded_at: AwareDatetime | None = Field(
        default=None, description="The earliest ledger record of the episode."
    )
    ledger_line: NonEmptyStr | None = None
    recorded_by: ProcessId | None = None
    last_recorded_at: AwareDatetime | None = None
    last_ledger_line: NonEmptyStr | None = None

    @property
    def episode_key(self) -> str:
        return str(self.episode_id)


def state_key_text(process_id: str, host: str) -> str:
    return f"{process_id}@{host}"


class ModelAutomationLivenessSnapshot(BaseModel):
    """The rows the fold reads as prior state and returns as changes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    states: tuple[ModelAutomationLivenessStateRow, ...] = ()
    runs: tuple[ModelAutomationRunRow, ...] = ()
    episodes: tuple[ModelAutomationAlarmEpisodeRow, ...] = ()

    def state(
        self, process_id: str, host: str
    ) -> ModelAutomationLivenessStateRow | None:
        return next(
            (s for s in self.states if s.key == (process_id, host)),
            None,
        )

    def episode(self, episode_id: UUID) -> ModelAutomationAlarmEpisodeRow | None:
        return next((e for e in self.episodes if e.episode_id == episode_id), None)

    def runs_for(self, process_id: str, host: str) -> tuple[ModelAutomationRunRow, ...]:
        return tuple(
            r for r in self.runs if r.process_id == process_id and r.host == host
        )

    def merged(
        self, changes: ModelAutomationLivenessSnapshot
    ) -> ModelAutomationLivenessSnapshot:
        """This snapshot with each changed row replacing the row it supersedes."""
        states = {s.key: s for s in self.states}
        states.update({s.key: s for s in changes.states})
        runs = {r.key: r for r in self.runs}
        runs.update({r.key: r for r in changes.runs})
        episodes = {e.episode_id: e for e in self.episodes}
        episodes.update({e.episode_id: e for e in changes.episodes})
        return ModelAutomationLivenessSnapshot(
            states=tuple(states[k] for k in sorted(states)),
            runs=tuple(runs[k] for k in sorted(runs)),
            episodes=tuple(episodes[k] for k in sorted(episodes, key=str)),
        )


__all__ = [
    "ModelAutomationAlarmEpisodeRow",
    "ModelAutomationLivenessSnapshot",
    "ModelAutomationLivenessStateRow",
    "ModelAutomationRunRow",
    "state_key_text",
]
