# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The andon stop rule: one event folded through the contract's machine, and ``stop_gate``.

The lifecycle lives in the ``state_machine`` of the stop reducer's contract. This
module builds the machine's context from the supplied facts and asks core's
``execute_transition``; it keeps no table of states or transitions. The exception
queue folds events here, the reducer node calls the same function, and the fill, the
placement decision and the worker-engine choice call ``stop_gate`` in process.
Every number comes in as an argument (the contract's ``config.andon_stop`` block,
the ``andon_stop`` overlay's policy); none is a literal here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from importlib.resources import files
from typing import Any

import yaml
from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from omnibase_core.models.reducer.payloads.model_payload_fsm_transition_action import (
    ModelPayloadFSMTransitionAction,
)
from omnibase_core.utils.util_fsm_executor import execute_transition

from omnimarket.models.stop_gate import (
    EnumFoldStatus,
    EnumReadingOrigin,
    EnumReadingVerdict,
    EnumStopEventKind,
    EnumStopGateOutcome,
    EnumStopGateReason,
    EnumStopMode,
    ModelAndonStopConfig,
    ModelStopEvent,
    ModelStopFoldResult,
    ModelStopGateRequest,
    ModelStopGateResult,
    ModelStopReading,
    ModelStopRecord,
)

_REDUCER_PACKAGE = "omnimarket.nodes.node_andon_stop_reducer"
_PLAIN_TRIGGERS = {
    EnumStopEventKind.LEASE_GRANTED: "lease_granted",
    EnumStopEventKind.LEASE_EXPIRED: "lease_expired",
    EnumStopEventKind.STALE_LIMIT_REACHED: "stale_limit_reached",
    EnumStopEventKind.COUNTERMEASURE_MERGED: "countermeasure_merged",
    EnumStopEventKind.HAND_FIX_RECORDED: "hand_fix_recorded",
    EnumStopEventKind.REPLACEMENTS_EXTENDED: "replacements_extended",
    EnumStopEventKind.DECLARED_FALSE: "declared_false",
}


def load_andon_stop_contract() -> tuple[ModelFSMSubcontract, ModelAndonStopConfig]:
    """The packaged reducer contract's state machine and ``config.andon_stop`` block.

    Read once at composition time and passed to the functions below.
    """
    raw = yaml.safe_load(
        files(_REDUCER_PACKAGE).joinpath("contract.yaml").read_text("utf-8")
    )
    return (
        ModelFSMSubcontract.model_validate(raw["state_machine"]),
        ModelAndonStopConfig.model_validate(raw["config"]["andon_stop"]),
    )


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def replacements_remaining(stop: ModelStopRecord, config: ModelAndonStopConfig) -> int:
    """The configured replacements plus any operator extension, less those used."""
    return (
        config.max_lease_replacements
        + stop.replacements_extended
        - stop.lease_replacements_used
    )


def replacements_used_limit(stop: ModelStopRecord, config: ModelAndonStopConfig) -> int:
    """The most replacements this stop may use: the configured count plus any operator extension."""
    return config.max_lease_replacements + stop.replacements_extended


def readback_trigger(reading: ModelStopReading) -> tuple[str, bool]:
    """The machine trigger a reading is sent as, and whether it counts toward clearing.

    Only a ``PASS`` that is proof eligible, executed and covers the stopped scope
    counts. A ``MISSING`` reading for the stopped scope is a mismatch whatever its
    binding; every other reading is unknown and holds the stop.
    """
    counted = (
        reading.verdict is EnumReadingVerdict.PASS
        and reading.proof_eligible
        and reading.origin is EnumReadingOrigin.EXECUTED
        and reading.covers_scope
    )
    if counted:
        return "readback_fixed", True
    if reading.verdict is EnumReadingVerdict.MISSING and reading.covers_scope:
        return "readback_mismatch", False
    return "readback_unknown", False


def _hand_fix_allowed(event: ModelStopEvent) -> bool:
    evidence = event.hand_fix
    return (
        evidence is not None
        and evidence.closed_fixed_by_hand
        and evidence.first_occurrence
        and bool(evidence.repair_evidence.strip())
    )


def _refused(
    stop: ModelStopRecord, event: ModelStopEvent, reason: str
) -> ModelStopFoldResult:
    return ModelStopFoldResult(
        status=EnumFoldStatus.REFUSED,
        stop=stop,
        reason=reason,
        line=f"REFUSED stop:{stop.id} event={event.kind.value} reason={reason}",
    )


def _stale_since(stop: ModelStopRecord) -> datetime:
    """When the wait began: the countermeasure for a fixed stop, else the stop itself."""
    return stop.fixed_at if stop.fixed_at is not None else stop.stopped_at


def _plan(
    stop: ModelStopRecord, event: ModelStopEvent, config: ModelAndonStopConfig
) -> tuple[str, dict[str, Any], str]:
    """The trigger, the machine's context and a refusal reason ('' when the event may be tried)."""
    kind = event.kind
    context: dict[str, Any] = {
        "lease_replacements_remaining": replacements_remaining(stop, config),
        "hand_fix_allowed": _hand_fix_allowed(event),
        "readback_complete": False,
        "fixed_readings_missing": config.consecutive_fixed_readings,
    }
    if kind is EnumStopEventKind.READBACK:
        if event.reading is None:
            return "", context, "reading_missing"
        trigger, counted = readback_trigger(event.reading)
        seen = stop.fixed_readings_seen + 1 if counted else 0
        context["readback_complete"] = counted
        context["fixed_readings_missing"] = max(
            0, config.consecutive_fixed_readings - seen
        )
        return trigger, context, ""
    trigger = _PLAIN_TRIGGERS[kind]
    reason = ""
    if kind is EnumStopEventKind.LEASE_GRANTED and not event.lane_id:
        reason = "lane_missing"
    elif kind is EnumStopEventKind.LEASE_EXPIRED:
        if stop.lease_expires_at is None or event.at <= stop.lease_expires_at:
            reason = "lease_not_expired"
        elif context["lease_replacements_remaining"] != 0 and not event.lane_id:
            reason = "lane_missing"
    elif kind is EnumStopEventKind.STALE_LIMIT_REACHED:
        live = stop.lease_expires_at is not None and stop.lease_expires_at > event.at
        waited = event.at - _stale_since(stop)
        if live or waited < timedelta(hours=config.stale_after_hours):
            reason = "stale_limit_not_reached"
    elif kind is EnumStopEventKind.REPLACEMENTS_EXTENDED and event.extension <= 0:
        reason = "extension_missing"
    return trigger, context, reason


def _next_record(
    stop: ModelStopRecord,
    event: ModelStopEvent,
    config: ModelAndonStopConfig,
    trigger: str,
    new_state: str,
    fsm: ModelFSMSubcontract,
) -> ModelStopRecord:
    changes: dict[str, Any] = {"state": new_state}
    lease_end = event.at + timedelta(minutes=config.lease_minutes)
    if trigger == "lease_granted":
        changes.update(swarm_lane=event.lane_id, lease_expires_at=lease_end)
    elif trigger == "lease_expired":
        if stop.lease_replacements_used < replacements_used_limit(stop, config):
            changes.update(
                swarm_lane=event.lane_id,
                lease_expires_at=lease_end,
                lease_replacements_used=stop.lease_replacements_used + 1,
            )
        else:
            changes.update(swarm_lane=None, lease_expires_at=None)
    elif trigger == "stale_limit_reached":
        changes.update(swarm_lane=None, lease_expires_at=None)
    elif trigger == "countermeasure_merged":
        changes.update(
            countermeasure_at=event.at, fixed_at=event.at, fixed_readings_seen=0
        )
    elif trigger == "hand_fix_recorded":
        changes.update(fixed_at=event.at, fixed_readings_seen=0)
    elif trigger == "replacements_extended":
        changes.update(
            replacements_extended=stop.replacements_extended + event.extension
        )
    elif trigger == "readback_fixed":
        changes["fixed_readings_seen"] = stop.fixed_readings_seen + 1
        if new_state in fsm.terminal_states:
            changes["cleared_at"] = event.at
    elif trigger in ("readback_unknown", "readback_mismatch"):
        changes["fixed_readings_seen"] = 0
        if new_state == fsm.initial_state:
            changes.update(swarm_lane=None, lease_expires_at=None, fixed_at=None)
    return stop.model_copy(update=changes)


async def fold_stop_event_async(
    stop: ModelStopRecord,
    event: ModelStopEvent,
    fsm: ModelFSMSubcontract,
    config: ModelAndonStopConfig,
) -> ModelStopFoldResult:
    expected = config.writer_roles[event.kind.value]
    if event.role != expected.value:
        return _refused(stop, event, f"role_not_allowed:{event.role or 'none'}")
    trigger, context, problem = _plan(stop, event, config)
    if problem:
        return _refused(stop, event, problem)
    try:
        result = await execute_transition(fsm, stop.state, trigger, context)
    except ModelOnexError:
        return _refused(stop, event, f"no_transition:{stop.state}:{trigger}")
    if not result.success:
        failed = ",".join(result.failed_conditions or ()) or "guard"
        return _refused(stop, event, f"guard_rejected:{failed}")
    new_state = result.new_state
    actions = tuple(
        intent.payload.action_name
        for intent in result.intents
        if isinstance(intent.payload, ModelPayloadFSMTransitionAction)
    )
    return ModelStopFoldResult(
        status=EnumFoldStatus.APPLIED,
        stop=_next_record(stop, event, config, trigger, new_state, fsm),
        trigger=trigger,
        transition=result.transition_name or "",
        actions=actions,
        line=(f"STOP {stop.id} class={stop.cls} state={new_state} trigger={trigger}"),
    )


def fold_stop_event(
    stop: ModelStopRecord,
    event: ModelStopEvent,
    fsm: ModelFSMSubcontract,
    config: ModelAndonStopConfig,
) -> ModelStopFoldResult:
    """Fold one writer's event into one stop through the contract's machine."""
    return _run(fold_stop_event_async(stop, event, fsm, config))


def _gate_one(
    stop: ModelStopRecord, request: ModelStopGateRequest
) -> ModelStopGateResult:
    named = f"stop:{stop.id} class={stop.cls} swarm={stop.swarm_lane or 'none'}"
    common: dict[str, Any] = {
        "stop_id": stop.id,
        "swarm_lane": stop.swarm_lane,
        "stop_class": stop.cls,
    }
    mode = request.policy.modes.get(stop.cls)
    if mode is None:
        return ModelStopGateResult(
            outcome=EnumStopGateOutcome.REFUSE,
            reason=EnumStopGateReason.MODE_MISSING,
            line=f"REFUSE {named} reason=mode_missing key=andon_stop.modes.{stop.cls}",
            **common,
        )
    if mode is EnumStopMode.OBSERVE:
        return ModelStopGateResult(
            outcome=EnumStopGateOutcome.ADMIT,
            reason=EnumStopGateReason.OBSERVED,
            mode=mode,
            line=f"ADMIT {named} mode=observe",
            **common,
        )
    holds_lease = (
        bool(request.lane_id)
        and request.lane_id == stop.swarm_lane
        and stop.lease_expires_at is not None
        and stop.lease_expires_at > request.now
    )
    if holds_lease:
        return ModelStopGateResult(
            outcome=EnumStopGateOutcome.ADMIT,
            reason=EnumStopGateReason.SWARM_LANE,
            mode=mode,
            line=f"ADMIT {named} mode=enforce reason=swarm_lane",
            **common,
        )
    return ModelStopGateResult(
        outcome=EnumStopGateOutcome.REFUSE,
        reason=EnumStopGateReason.STOP_OPEN,
        mode=mode,
        line=f"REFUSE {named} mode=enforce",
        **common,
    )


def stop_gate(
    request: ModelStopGateRequest, fsm: ModelFSMSubcontract
) -> ModelStopGateResult:
    """Admit or refuse new work into ``request.scope`` from the open stops and the overlay modes.

    A stop in a terminal state of the machine (cleared, declared false) blocks nothing. A class in ``observe`` admits with the
    stop named; a class with no mode refuses naming the overlay key; the swarm lane
    holding a live lease is never refused by its own stop.
    """
    blocking = sorted(
        (
            stop
            for stop in request.stops
            if stop.scope == request.scope and stop.state not in fsm.terminal_states
        ),
        key=lambda stop: (stop.stopped_at, stop.id),
    )
    if not blocking:
        return ModelStopGateResult(
            outcome=EnumStopGateOutcome.ADMIT,
            reason=EnumStopGateReason.NO_STOP,
            line=f"ADMIT stop-gate scope={request.scope.kind}:{request.scope.key} reason=no_stop",
        )
    results = [_gate_one(stop, request) for stop in blocking]
    return next(
        (r for r in results if r.outcome is EnumStopGateOutcome.REFUSE), results[0]
    )
