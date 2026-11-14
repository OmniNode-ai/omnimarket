# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tick handler of node_dev_roll_window_gate_effect (OMN-19998).

On each runtime tick with commands held, asks the batching decision of
node_dev_roll_request_compute (the pure ``decide_dev_roll``) whether to roll now. A hold
changes nothing. Roll now releases EVERY held command the decision covered, oldest first,
each marked released so the deploy effect publishes it without holding it again; the
agent's own queue folding then makes one roll of the batch. The state (held commands,
the roll started, the released ids) is saved before the commands are returned: the
release is at most once. A crash between the save and the runtime's publish drops that
batch's commands, and the next dev merge's command carries the lane forward to a newer
ref; a repeat of a released command is refused by the deploy effect's repeat record.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from uuid import uuid4

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import BaseModel

from omnimarket.models.dev_roll import (
    EnumDevRollAction,
    EnumDevRollHealth,
    ModelDevRollFacts,
    ModelDevRollRecord,
    ModelDevTipChanged,
)
from omnimarket.nodes.node_dev_roll_request_compute.handlers import decide_dev_roll
from omnimarket.nodes.node_dev_roll_request_compute.models import ModelDevRollRefused
from omnimarket.nodes.node_dev_roll_window_gate_effect.handlers.gate_context import (
    DevRollGateContext,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    ModelDevRollGateDecision,
    ModelDevRollWindowGatePolicy,
    ModelHeldDevRoll,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.protocols import (
    ProtocolDevRollGateStore,
)


def _merged_at(held: ModelHeldDevRoll) -> datetime:
    return held.command.requested_at or held.held_at


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

    def handle(self, request: ModelRuntimeTick) -> Sequence[BaseModel] | None:
        policy = self._context.policy()
        if policy.mode == "off":
            return None
        store = self._context.store()
        state = store.load()
        if not state.held:
            return None
        now = self._context.now()
        facts = ModelDevRollFacts(
            correlation_id=uuid4(),
            observed_at=now,
            pending_events=tuple(
                ModelDevTipChanged(
                    event_id=held.command.correlation_id,
                    repository=held.repository,
                    sha=held.sha,
                    merged_at=_merged_at(held),
                )
                for held in state.held
            ),
            last_roll=state.last_roll,
        )
        decided = decide_dev_roll(facts, policy.to_roll_policy())
        if isinstance(decided, ModelDevRollRefused):
            return [
                ModelDevRollGateDecision(
                    correlation_id=facts.correlation_id,
                    action="refused",
                    hold_reason=decided.reason.value,
                    hold_until=None,
                    held_count=len(state.held),
                    released_count=0,
                    longest_wait_seconds=0,
                    observed_at=now,
                )
            ]
        if decided.action is not EnumDevRollAction.ROLL_NOW:
            return [
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
                )
            ]
        covered = set(decided.rolled_event_ids)
        released = sorted(
            (h for h in state.held if h.command.correlation_id in covered),
            key=lambda h: (_merged_at(h), str(h.command.correlation_id)),
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
            h.command.model_copy(update={"roll_window_released": True})
            for h in released
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
