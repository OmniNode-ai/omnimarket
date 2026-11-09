# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The effects-held condition: no live consumer for the delegation topic past its bound opens a lane (OMN-23106).

On 2026-11-07 the runtime-effects consumer, the only reader of the one-partition delegate-skill topic, was
absent from 20:01Z to 03:04Z. The alarm raised at 20:16Z, wrote a ledger row, and nothing acted on it: 45
requests waited seven hours. The rule here is the alarm script's, unchanged: ALARM when no group matching
the declared prefix and suffix is Stable with a live member for at least the bound; OK while one is live or
while the hold is under the bound; INDETERMINATE only when the groups cannot be read, which also restarts the
hold clock. What is new is that an ALARM acts: once per episode the injected dispatcher starts one lane with a
brief naming the condition and when it began. A dispatch that cannot start is not an episode's lane: the
next tick tries again and the run says ``failed`` until one starts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_effects_held import (
    EnumEffectsHeldFault,
    EnumEffectsHeldOutcome,
    EnumEscalation,
    ModelEffectsGroupReading,
    ModelEffectsHeldCommand,
    ModelEffectsHeldResult,
    ModelEscalationBrief,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelEffectsHeldSettings
from omnimarket.nodes.node_lab_alarm.protocols import (
    EffectsGroupReadError,
    LaneDispatchError,
    ProtocolEffectsGroupReader,
    ProtocolHeldStateStore,
    ProtocolLaneDispatcher,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"

CONDITION = "effects_held_behind_runtime"
SUBJECT = "runtime-effects"
_LIVE_STATE = "Stable"
_HELD_SINCE = "held_since"
_ESCALATED_AT = "escalated_at"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class HandlerEffectsHeld:
    """Read the effects groups, keep the hold clock, and open one lane per episode that passes the bound."""

    def __init__(
        self,
        *,
        settings: ModelEffectsHeldSettings,
        groups: ProtocolEffectsGroupReader,
        state: ProtocolHeldStateStore,
        dispatcher: ProtocolLaneDispatcher,
        clock: Callable[[], datetime],
    ) -> None:
        self._settings = settings
        self._groups = groups
        self._state = state
        self._dispatcher = dispatcher
        self._clock = clock

    def handle(self, request: ModelEffectsHeldCommand) -> ModelEffectsHeldResult:
        drill = request.fault is not EnumEffectsHeldFault.NONE
        keep = request.write and not drill
        now = self._clock()
        state = dict(self._state.load())
        try:
            readings = self._groups.read()
        except EffectsGroupReadError as exc:
            state.pop(_HELD_SINCE, None)
            state.pop(_ESCALATED_AT, None)
            return self._finish(
                request,
                EnumEffectsHeldOutcome.INDETERMINATE,
                (),
                state,
                keep,
                reasons=(f"unreadable:{exc}",),
                evidence=f"the effects consumer groups could not be read: {exc}",
            )
        if not readings:
            state.pop(_HELD_SINCE, None)
            state.pop(_ESCALATED_AT, None)
            return self._finish(
                request,
                EnumEffectsHeldOutcome.INDETERMINATE,
                (),
                state,
                keep,
                reasons=("no-group-matched",),
                evidence="no group matched the declared prefix and suffix; a renamed or undeclared group is not a live one",
            )
        if request.fault is EnumEffectsHeldFault.CONSUMER_ABSENT:
            readings = tuple(
                ModelEffectsGroupReading(group=r.group, state="Empty", members=0)
                for r in readings
            )
        listed = "; ".join(f"{r.group}: {r.state}/{r.members}" for r in readings)
        if any(r.state == _LIVE_STATE and r.members > 0 for r in readings):
            state.pop(_HELD_SINCE, None)
            state.pop(_ESCALATED_AT, None)
            return self._finish(
                request,
                EnumEffectsHeldOutcome.OK,
                readings,
                state,
                keep,
                evidence=f"{listed}; at least one group is Stable with a live member",
            )
        bound = self._settings.held_after_seconds
        since_text = state.get(_HELD_SINCE) or _stamp(now)
        state[_HELD_SINCE] = since_text
        held_for = (now - datetime.fromisoformat(since_text)).total_seconds()
        if request.fault is EnumEffectsHeldFault.CONSUMER_ABSENT:
            held_for = max(held_for, bound)
        if held_for < bound:
            return self._finish(
                request,
                EnumEffectsHeldOutcome.OK,
                readings,
                state,
                keep,
                held_since=since_text,
                held_for=held_for,
                evidence=f"{listed}; no live member since {since_text} ({held_for:.0f} s), under the {bound:.0f} s bound",
            )
        evidence = (
            f"{listed}; no live member for {held_for:.0f} s (since {since_text}), "
            f"at or past the {bound:.0f} s bound: delegation cannot be processed while this holds"
        )
        escalation, reasons = self._escalate(
            state, since_text, evidence, keep=keep, drill=drill, now=now
        )
        return self._finish(
            request,
            EnumEffectsHeldOutcome.ALARM,
            readings,
            state,
            keep,
            held_since=since_text,
            held_for=held_for,
            escalation=escalation,
            reasons=reasons,
            evidence=evidence,
        )

    def _escalate(
        self,
        state: dict[str, str],
        since_text: str,
        evidence: str,
        *,
        keep: bool,
        drill: bool,
        now: datetime,
    ) -> tuple[EnumEscalation, tuple[str, ...]]:
        if drill:
            return EnumEscalation.DRILL, ()
        if _ESCALATED_AT in state:
            return EnumEscalation.ALREADY, ()
        if not keep:
            # A probe reports what a tick would do and starts nothing.
            return EnumEscalation.NOT_NEEDED, ("probe-opens-no-lane",)
        brief = ModelEscalationBrief(
            condition=CONDITION,
            subject=SUBJECT,
            raised_at=since_text,
            detail="the delegate-skill consumer has had no live member past its bound",
            evidence=evidence,
        )
        try:
            self._dispatcher.dispatch(brief)
        except LaneDispatchError as exc:
            return EnumEscalation.FAILED, (f"dispatch:{exc}",)
        state[_ESCALATED_AT] = _stamp(now)
        return EnumEscalation.DISPATCHED, ()

    def _finish(
        self,
        request: ModelEffectsHeldCommand,
        outcome: EnumEffectsHeldOutcome,
        readings: tuple[ModelEffectsGroupReading, ...],
        state: Mapping[str, str],
        keep: bool,
        *,
        held_since: str = "",
        held_for: float = 0.0,
        escalation: EnumEscalation = EnumEscalation.NOT_NEEDED,
        reasons: tuple[str, ...] = (),
        evidence: str,
    ) -> ModelEffectsHeldResult:
        if keep:
            self._state.save(state)
        failure = ""
        if outcome is EnumEffectsHeldOutcome.ALARM:
            failure = f"alarm:{CONDITION};escalation={escalation.value}" + "".join(
                f";{r}" for r in reasons
            )
        elif outcome is EnumEffectsHeldOutcome.INDETERMINATE:
            failure = "indeterminate:" + ";".join(reasons)
        return ModelEffectsHeldResult(
            correlation_id=request.correlation_id,
            outcome=outcome,
            fault=request.fault,
            readings=readings,
            held_since=held_since,
            held_for_seconds=held_for,
            bound_seconds=self._settings.held_after_seconds,
            escalation=escalation,
            reasons=reasons,
            state_kept=keep,
            evidence=evidence,
            failure=failure,
        )


def events_for(
    result: ModelEffectsHeldResult, topics: Mapping[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The chain of one run: started, one reading per group, the lane when one was opened, then the terminal."""
    cid = result.correlation_id
    chain: list[tuple[str, dict[str, object]]] = [
        (topics["effects_held_run_started"], {"correlation_id": cid})
    ]
    chain.extend(
        (
            topics["effects_held_group_read"],
            {
                "correlation_id": cid,
                "group": r.group,
                "state": r.state,
                "members": r.members,
            },
        )
        for r in result.readings
    )
    if result.escalation in (EnumEscalation.DISPATCHED, EnumEscalation.FAILED):
        chain.append(
            (
                topics["effects_held_lane_dispatch"],
                {
                    "correlation_id": cid,
                    "condition": CONDITION,
                    "escalation": result.escalation.value,
                    "raised_at": result.held_since,
                    "reasons": list(result.reasons),
                },
            )
        )
    final = topics[
        "effects_held_run_completed"
        if result.outcome is EnumEffectsHeldOutcome.OK
        else "effects_held_run_failed"
    ]
    chain.append(
        (
            final,
            {
                "correlation_id": cid,
                "outcome": result.outcome.value,
                "fault": result.fault.value,
                "held_since": result.held_since,
                "held_for_seconds": result.held_for_seconds,
                "bound_seconds": result.bound_seconds,
                "escalation": result.escalation.value,
                "reasons": list(result.reasons),
                "state_kept": result.state_kept,
                "failure": result.failure,
            },
        )
    )
    return chain


__all__ = [
    "CONDITION",
    "SUBJECT",
    "HandlerEffectsHeld",
    "events_for",
    "load_topics",
]
