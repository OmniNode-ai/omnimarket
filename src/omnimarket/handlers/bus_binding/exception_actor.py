# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the bus-binding exception classes in code; emit only the case code cannot resolve (OMN-21959).

Operator process ruling 2026-10-18: a detector and an actor resolve their case in code, or emit one typed
exception row; the second occurrence of a class becomes code. This is that actor for ``bus-binding-degraded``,
``bus-binding-outage`` and ``bus-binding-outbox-refused``.

* degraded and outage: the durable health read is retried and the outbox replayed (two ports the caller binds
  to the real bus); the case is resolved when the read passes and nothing remains queued.
* outbox-refused: a refusal that names equal ledger and request digests is a duplicate that was settled; one
  that names different digests, or none, is a real clash a person rules on and is emitted.

A row marked as a decision is never resolved here. A resolved case that is open in the queue is closed there.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from omnimarket.handlers.bus_binding import outbox
from omnimarket.handlers.bus_binding.detector import classify_block
from omnimarket.handlers.bus_binding.recovery import (
    ALARM_CLASS,
    OUTAGE_CLASS,
    REFUSED_CLASS,
    SOURCE,
    read_record,
    settlement_policy,
)
from omnimarket.models.bus_binding import (
    EnumBusBinding,
    EnumBusBindingState,
    ModelBusBindingDecision,
    ModelBusBindingResolution,
)
from omnimarket.nodes.node_exception_queue_effect.container.container_exception_queue_effect import (
    create_handler_exception_emit,
    create_handler_exception_queue,
    create_handler_exception_resolve,
)
from omnimarket.nodes.node_exception_queue_effect.models import (
    ModelExceptionEmitRequest,
    ModelExceptionQueueRequest,
    ModelExceptionResolveRequest,
    ModelExceptionRow,
)

_DIGESTS = re.compile(
    r"ledger digest (?P<ledger>[0-9a-f]{64}), request digest (?P<request>[0-9a-f]{64})"
)
_NO_IDENTITY = "holds no identity"


@dataclass(frozen=True)
class ActorPorts:
    """What the actor may do to the world: both are bounded and report, they never raise for a down bus."""

    durable_healthy: Callable[[], bool]
    replay_outbox: Callable[
        [], int
    ]  # replays the local outbox; returns how many requests remain


def _stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_binding(row: Mapping[str, object], ports: ActorPorts) -> tuple[bool, str]:
    """(resolved, how or what was tried) for a degraded or outage case."""
    if not ports.durable_healthy():
        if _NO_IDENTITY in str(row.get("evidence", "")):
            return False, (
                "retried the durable health read; it still fails for want of a lane identity, "
                "which retry cannot issue"
            )
        return False, "retried the durable health read; it still fails"
    remaining = ports.replay_outbox()
    if remaining:
        return False, f"durable health read passed; replay left {remaining} queued"
    return True, "durable health read passed; outbox replayed, 0 queued"


def _resolve_refusal(row: Mapping[str, object]) -> tuple[bool, str]:
    """(resolved, how or what was tried) for an outbox-refused case."""
    digests = _DIGESTS.search(str(row.get("evidence", "")))
    if digests is None:
        return (
            False,
            "read the refusal; it names no digests, so the clash cannot be told from a replay",
        )
    if digests["ledger"] == digests["request"]:
        return (
            True,
            f"refusal names equal digests {digests['ledger']}: a settled duplicate",
        )
    return (
        False,
        "read the refusal; the ledger and the request hold different rows under one id",
    )


def settle_case(
    state_dir: Path,
    row: Mapping[str, object],
    ports: ActorPorts,
    *,
    now: datetime,
) -> ModelBusBindingResolution:
    """Resolve one case in code, or emit it once to the exception queue under ``state_dir``."""
    cls = classify_block(row)
    if cls is None:
        return ModelBusBindingResolution(
            cls="", resolved=False, how="not a bus-binding class", emitted=""
        )
    if row.get("decision"):
        return ModelBusBindingResolution(
            cls=cls,
            resolved=False,
            how="decision row: left to the operator",
            emitted="",
        )
    resolved, how = (
        _resolve_refusal(row) if cls == REFUSED_CLASS else _resolve_binding(row, ports)
    )
    queue_dir = state_dir / "exception-queue"
    if resolved:
        case_id = str(row.get("id", ""))
        if case_id:
            create_handler_exception_resolve().handle(
                ModelExceptionResolveRequest(
                    queue_dir, case_id, SOURCE, how, _stamp(now)
                )
            )
        return ModelBusBindingResolution(cls=cls, resolved=True, how=how, emitted="")
    fix = str(row.get("proposed_fix", ""))
    emitted = create_handler_exception_emit().handle(
        ModelExceptionEmitRequest(
            state_dir=queue_dir,
            row=ModelExceptionRow(
                cls=cls,
                key=str(row.get("key", "")),
                source=SOURCE,
                evidence=str(row.get("evidence", "")),
                tried=how,
                proposed_fix=fix
                or (
                    "investigate the prolonged durable binding outage; automatic recovery remains enabled"
                    if cls == OUTAGE_CLASS
                    else "automatic recovery: retry the durable health read and replay the local outbox"
                    if cls == ALARM_CLASS
                    else "correct the row and append it again; the refused request is final"
                ),
                decision=False,
            ),
            now=_stamp(now),
        )
    )
    return ModelBusBindingResolution(
        cls=cls, resolved=False, how=how, emitted=f"{emitted.status}:{emitted.id}"
    )


def settle_open_cases(
    state_dir: Path, decision: ModelBusBindingDecision
) -> tuple[ModelBusBindingResolution, ...]:
    """Settle this lane's open rows of the contract's classes after a healthy durable command (OMN-21959).

    The bus binding calls this once its command is done. It acts only when this command's healthy read is
    still the lane's latest observation and the outbox is empty, so the cause is gone; a row opened by an
    earlier process, or one whose episode this process no longer remembers, is then resolved here instead of
    staying open until someone resolves it by hand. A decision row is never touched.
    """
    classes = settlement_policy().settle_on_healthy_read
    if (
        not classes
        or decision.binding is not EnumBusBinding.DURABLE
        or decision.degraded
    ):
        return ()
    record = read_record(state_dir, decision.lane)
    if (
        record.get("state") == EnumBusBindingState.DEGRADED.value
        or record.get("observed_at") != decision.observed_at.isoformat()
    ):
        return ()
    # Include unreadable request files: ignoring one would falsely claim complete replay.
    if any(outbox.outbox_dir(state_dir).glob("*.json")):
        return ()
    queue_dir = state_dir / "exception-queue"
    if not queue_dir.is_dir():
        return ()
    rows = (
        create_handler_exception_queue()
        .handle(ModelExceptionQueueRequest(queue_dir, "orchestrator"))
        .rows
    )
    ports = ActorPorts(durable_healthy=lambda: True, replay_outbox=lambda: 0)
    return tuple(
        settle_case(state_dir, row, ports, now=decision.observed_at)
        for row in rows
        if row.get("cls") in classes
        and row.get("source") == SOURCE
        and row.get("key") == f"lane={decision.lane}"
        and not row.get("decision")
    )
