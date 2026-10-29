# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Persist a fallback episode until a healthy binding has replayed its outbox."""

from __future__ import annotations

import json
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.bus_binding import outbox
from omnimarket.models.bus_binding import (
    EnumBusBinding,
    EnumBusBindingState,
    ModelBusBindingDecision,
)
from omnimarket.nodes.node_exception_queue_effect.handler import (
    HandlerExceptionEmit,
    HandlerExceptionQueue,
    HandlerExceptionResolve,
)
from omnimarket.nodes.node_exception_queue_effect.models import (
    ModelExceptionEmitRequest,
    ModelExceptionQueueRequest,
    ModelExceptionResolveRequest,
    ModelExceptionRow,
)

ALARM_CLASS = "bus-binding-degraded"
OUTAGE_CLASS = "bus-binding-outage"
REFUSED_CLASS = "bus-binding-outbox-refused"
SOURCE = "omnimarket.handlers.bus_binding"


def state_path(state_dir: Path, lane: str) -> Path:
    return state_dir / "bus-binding" / f"{lane}.json"


def read_record(state_dir: Path, lane: str) -> dict[str, Any]:
    try:
        record: dict[str, Any] = json.loads(
            state_path(state_dir, lane).read_text(encoding="utf-8")
        )
        return record
    except (OSError, ValueError):
        return {}


def _write_record(state_dir: Path, lane: str, record: dict[str, Any]) -> None:
    path = state_path(state_dir, lane)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def observe(state_dir: Path, decision: ModelBusBindingDecision) -> None:
    """Keep the start and replay totals across process restarts and intermediate reads."""
    record = read_record(state_dir, decision.lane)
    started = record.get("outage_started_at")
    if not started and decision.degraded:
        # Retain the best known start for a state file written by the earlier binding.
        started = (
            record.get("since")
            if "outage_started_at" not in record
            and decision.prior_state is not EnumBusBindingState.HEALTHY
            else None
        ) or decision.observed_at.isoformat()
    record.update(
        lane=decision.lane,
        state=decision.state.value,
        binding=decision.binding.value,
        reason=decision.reason,
        observed_at=decision.observed_at.isoformat(),
        since=started or decision.observed_at.isoformat(),
        outage_started_at=started,
    )
    _write_record(state_dir, decision.lane, record)


def raise_alarm(
    state_dir: Path,
    decision: ModelBusBindingDecision,
    *,
    outage_seconds: float | None = None,
) -> str:
    """A normal exception is self-healing; an overdue one asks the orchestrator to investigate."""
    overdue = outage_seconds is not None
    evidence = (
        f"outage_seconds={outage_seconds:g}; durable bus health read failed: {decision.reason}"
        if overdue
        else f"durable bus health read failed: {decision.reason}"
    )
    result = HandlerExceptionEmit().handle(
        ModelExceptionEmitRequest(
            state_dir=state_dir / "exception-queue",
            row=ModelExceptionRow(
                cls=OUTAGE_CLASS if overdue else ALARM_CLASS,
                key=f"lane={decision.lane}",
                source=SOURCE,
                evidence=evidence,
                tried="fell back to the in-memory event bus; ledger appends queue to the local outbox",
                proposed_fix=(
                    "investigate the prolonged durable binding outage; automatic recovery remains enabled"
                    if overdue
                    else "automatic recovery: retry the durable health read and replay the local outbox"
                ),
                decision=False,
            ),
            now=decision.observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )
    return f"{result.status}:{result.id}"


def escalation_bound_seconds() -> float:
    contract = yaml.safe_load(
        resources.files("omnimarket.nodes.node_bus_binding_reducer")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    bound = float(contract["recovery_policy"]["escalate_after_seconds"])
    if not 0 < bound < float("inf"):
        raise ValueError("bus binding escalation bound must be finite and positive")
    return bound


def escalate_if_overdue(
    state_dir: Path, decision: ModelBusBindingDecision
) -> str | None:
    record = read_record(state_dir, decision.lane)
    started = record.get("outage_started_at")
    if not started:
        return None
    elapsed = max(
        0.0, (decision.observed_at - datetime.fromisoformat(started)).total_seconds()
    )
    if elapsed > escalation_bound_seconds():
        return raise_alarm(state_dir, decision, outage_seconds=elapsed)
    return None


def record_replay(
    state_dir: Path, decision: ModelBusBindingDecision, result: outbox.DrainResult
) -> None:
    record = read_record(state_dir, decision.lane)
    if not record.get("outage_started_at"):
        return
    for field in ("replayed", "duplicates", "refused"):
        record[field] = record.get(field, 0) + getattr(result, field)
    record["replay_stopped"] = result.stopped
    _write_record(state_dir, decision.lane, record)
    resolve_if_recovered(state_dir, decision)


def _report_refused(
    state_dir: Path, decision: ModelBusBindingDecision, started: str
) -> int:
    """Emit one non-decision case per request refused in this episode; return how many."""
    directory = outbox.outbox_dir(state_dir) / outbox.REFUSED_DIRNAME
    if not directory.is_dir():
        return 0
    since_ns = int(datetime.fromisoformat(started).timestamp() * 1_000_000_000)
    count = 0
    for path in sorted(directory.glob("*.json")):
        try:
            queued_ns = int(path.stem.split("_", 1)[0])
            request_id = outbox.request_id_of(path)
        except (ValueError, IndexError):
            continue
        # Requests are queued while degraded, so a file older than the episode belongs to an earlier one.
        if queued_ns < since_ns:
            continue
        try:
            refusal = json.loads(path.read_text(encoding="utf-8")).get("refusal", {})
            message = " ".join(str(refusal.get("message", "")).split())[:300]
        except (OSError, ValueError, AttributeError):
            message = "refusal unreadable"
        HandlerExceptionEmit().handle(
            ModelExceptionEmitRequest(
                state_dir=state_dir / "exception-queue",
                row=ModelExceptionRow(
                    cls=REFUSED_CLASS,
                    key=f"lane={decision.lane} request={request_id}",
                    source=SOURCE,
                    evidence=f"ledger host refused the queued append: {message}",
                    tried=f"replayed the outbox; kept the refused request at {path}",
                    proposed_fix="correct the row and append it again; the refused request is final",
                    decision=False,
                ),
                now=decision.observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        )
        count += 1
    return count


def resolve_if_recovered(state_dir: Path, decision: ModelBusBindingDecision) -> None:
    """Resolve only this lane's open non-decision rows after a healthy read and complete replay."""
    if decision.binding is not EnumBusBinding.DURABLE or decision.degraded:
        return
    record = read_record(state_dir, decision.lane)
    if (
        not record.get("outage_started_at")
        or record.get("state") == EnumBusBindingState.DEGRADED.value
        or record.get("observed_at") != decision.observed_at.isoformat()
    ):
        return
    # A refusal is the ledger host's final answer and the request sits in refused/, so it does not hold
    # the episode open: it is reported below as its own case. A stopped drain or an unreplayed file does.
    if record.get("replay_stopped"):
        return
    # Include unreadable request files: ignoring one would falsely claim complete replay.
    if any(outbox.outbox_dir(state_dir).glob("*.json")):
        return
    queue_dir = state_dir / "exception-queue"
    refused = _report_refused(state_dir, decision, record["outage_started_at"])
    opened = HandlerExceptionQueue().handle(
        ModelExceptionQueueRequest(queue_dir, "orchestrator")
    )
    how = (
        f"durable_health_ok observed_at={decision.observed_at.isoformat()} "
        f"replayed={record.get('replayed', 0)} duplicates={record.get('duplicates', 0)} "
        f"refused={refused} remaining=0"
    )
    for row in opened.rows:
        if (
            row["cls"] in (ALARM_CLASS, OUTAGE_CLASS)
            and row["key"] == f"lane={decision.lane}"
            and row["source"] == SOURCE
        ):
            HandlerExceptionResolve().handle(
                ModelExceptionResolveRequest(
                    queue_dir,
                    row["id"],
                    SOURCE,
                    how,
                    decision.observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
            )
    record.update(
        outage_started_at=None, replayed=0, duplicates=0, refused=0, replay_stopped=None
    )
    _write_record(state_dir, decision.lane, record)
