# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tick handler of node_dev_roll_window_gate_effect (OMN-19998).

On each runtime tick with commands held, asks the batching decision of
node_dev_roll_request_compute (the pure ``decide_dev_roll``) whether to roll now. A hold
changes nothing. Roll now releases EVERY held command the decision covered, oldest first,
each marked released so the deploy effect publishes it without holding it again; the
agent's own queue folding then makes one roll of the batch. A roll no completion reaches by the
overlay's ``roll_completion_deadline_seconds`` is abandoned (a typed record in the state) and the held
commands go to the next roll in the same tick. The state (held commands,
the roll started, the released ids) is saved before the commands are returned: the
release is at most once. A crash between the save and the runtime's publish, or a
producer failure part-way through the batch (the redelivered tick finds nothing held),
drops the commands not yet published, and the next dev merge's command carries the lane forward to a newer
ref; a repeat of a released command is refused by the deploy effect's repeat record.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from uuid import uuid4

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import BaseModel

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.models.dev_roll import (
    EnumDevRollAction,
    EnumDevRollHealth,
    ModelDevRollFacts,
    ModelDevRollRecord,
    ModelDevRollRefused,
    ModelDevTipChanged,
)
from omnimarket.nodes.node_dev_roll_request_compute.handlers import decide_dev_roll
from omnimarket.nodes.node_dev_roll_window_gate_effect.handlers.gate_context import (
    DevRollGateContext,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.handlers.handler_dev_roll_window_held_age import (
    abandon_after_seconds,
    bounded_last_roll,
    held_age_alarm,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    EnumDevRollGateAbandonReason,
    EnumDevRollGateRollOutcome,
    ModelDevRollGateAbandonedRoll,
    ModelDevRollGateDecision,
    ModelDevRollGateRollRecorded,
    ModelDevRollGateState,
    ModelDevRollWindowGatePolicy,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.protocols import (
    ProtocolDevRollGateStore,
)


class HandlerDevRollWindowTick:
    """``handle(ModelRuntimeTick) -> None | ModelDevRollGateDecision | the released commands and the decision``."""

    def __init__(
        self,
        store: ProtocolDevRollGateStore | None = None,
        *,
        policy: ModelDevRollWindowGatePolicy | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._context = DevRollGateContext(
            store, policy=policy, environ=environ, clock=clock
        )

    def _flush(
        self, state: ModelDevRollGateState, store: ProtocolDevRollGateStore
    ) -> Sequence[BaseModel] | None:
        """The gate was switched off: release whatever it still holds, oldest first, so nothing is stranded."""
        if not state.held:
            return None
        flushed = sorted(
            state.held, key=lambda h: (h.merged_at, str(h.command.correlation_id))
        )
        store.save(state.model_copy(update={"held": ()}))
        return [
            h.command.model_copy(update={"roll_window_released": True}) for h in flushed
        ]

    def _abandon_unfinished_roll(
        self,
        state: ModelDevRollGateState,
        policy: ModelDevRollWindowGatePolicy,
        now: datetime,
        store: ProtocolDevRollGateStore,
    ) -> tuple[ModelDevRollGateState, ModelDevRollGateRollRecorded | None]:
        """Give up on a released roll no completion reached by the deadline, instead of holding every later release for ever.

        The roll is recorded in the state as a typed abandoned roll and cleared as the last roll, so the batching
        decision sees no roll to wait for and the held commands go to the next roll in this tick. A roll the earlier
        deadline code had already ended as not healthy (finished, health unknown: the agent's completion only ever
        reads healthy or unhealthy) is abandoned the same way.
        """
        last = state.last_roll
        if (
            last is None
            or last.runtime_started_at is None
            or last.health is not EnumDevRollHealth.UNKNOWN
        ):
            return state, None
        deadline_at = last.runtime_started_at + timedelta(
            seconds=abandon_after_seconds(policy)
        )
        if last.finished_at is None and now < deadline_at:
            return state, None
        abandoned = ModelDevRollGateAbandonedRoll(
            command_correlation_id=last.job_id,
            git_ref=last.git_ref,
            reason=EnumDevRollGateAbandonReason.COMPLETION_DEADLINE_PASSED,
            runtime_started_at=last.runtime_started_at,
            abandoned_at=last.finished_at or deadline_at,
        )
        ended = state.model_copy(
            update={
                "last_roll": None,
                "abandoned": (*state.abandoned, abandoned)[-policy.held_kept :],
            }
        )
        store.save(ended)
        return ended, ModelDevRollGateRollRecorded(
            command_correlation_id=last.job_id,
            outcome=EnumDevRollGateRollOutcome.ABANDONED,
            observed_at=now,
        )

    def handle(self, request: ModelRuntimeTick) -> Sequence[BaseModel] | None:
        try:
            policy = self._context.policy()
        except NodeOverlayNotFoundError:
            return (
                None  # no gate overlay installed: nothing to hold, nothing to release
            )
        store = self._context.store()
        state = store.load()
        now = self._context.now()
        if policy.mode == "off":
            return self._flush(state, store)
        blocking_roll = state.last_roll
        state, overdue = self._abandon_unfinished_roll(state, policy, now, store)
        if not state.held:
            return None if overdue is None else [overdue]
        lead: list[BaseModel] = [] if overdue is None else [overdue]
        alarm, alarmed_ids = held_age_alarm(state, blocking_roll, policy, now)
        if alarm is not None:
            state = state.model_copy(update={"alarmed_ids": alarmed_ids})
            store.save(state)
            lead.append(alarm)
        facts = ModelDevRollFacts(
            correlation_id=uuid4(),
            observed_at=now,
            pending_events=tuple(
                ModelDevTipChanged(
                    event_id=held.command.correlation_id,
                    repository=held.repository,
                    sha=held.sha,
                    merged_at=held.merged_at,
                )
                for held in state.held
            ),
            last_roll=bounded_last_roll(state.last_roll),
        )
        decided = decide_dev_roll(facts, policy.to_roll_policy())
        if isinstance(decided, ModelDevRollRefused):
            return [
                *lead,
                ModelDevRollGateDecision(
                    correlation_id=facts.correlation_id,
                    action="refused",
                    hold_reason=decided.reason.value,
                    hold_until=None,
                    held_count=len(state.held),
                    released_count=0,
                    longest_wait_seconds=0,
                    observed_at=now,
                ),
            ]
        if decided.action is not EnumDevRollAction.ROLL_NOW:
            return [
                *lead,
                ModelDevRollGateDecision(
                    correlation_id=facts.correlation_id,
                    action="hold",
                    hold_reason=(
                        decided.hold_reason.value if decided.hold_reason else None
                    ),
                    hold_until=decided.hold_until,
                    held_count=len(state.held),
                    released_count=0,
                    longest_wait_seconds=decided.longest_wait_seconds,
                    observed_at=now,
                ),
            ]
        covered = set(decided.rolled_event_ids)
        released = sorted(
            (h for h in state.held if h.command.correlation_id in covered),
            key=lambda h: (h.merged_at, str(h.command.correlation_id)),
        )
        remaining = tuple(
            h for h in state.held if h.command.correlation_id not in covered
        )
        newest = released[-1]
        released_ids = (
            *state.released_ids,
            *(str(h.command.correlation_id) for h in released),
        )[-policy.held_kept :]
        store.save(
            state.model_copy(
                update={
                    "held": remaining,
                    "released_ids": released_ids,
                    "alarmed_ids": tuple(
                        i
                        for i in state.alarmed_ids
                        if i in {str(h.command.correlation_id) for h in remaining}
                    ),
                    "last_roll": ModelDevRollRecord(
                        job_id=str(newest.command.correlation_id),
                        git_ref=newest.sha,
                        runtime_started_at=now,
                        finished_at=None,
                        health=EnumDevRollHealth.UNKNOWN,
                        healthy_since=None,
                    ),
                }
            )
        )
        commands: list[BaseModel] = [
            *lead,
            *(
                h.command.model_copy(update={"roll_window_released": True})
                for h in released
            ),
        ]
        commands.append(
            ModelDevRollGateDecision(
                correlation_id=facts.correlation_id,
                action="roll_now",
                hold_reason=None,
                hold_until=None,
                held_count=len(remaining),
                released_count=len(released),
                longest_wait_seconds=decided.longest_wait_seconds,
                observed_at=now,
            )
        )
        return commands
