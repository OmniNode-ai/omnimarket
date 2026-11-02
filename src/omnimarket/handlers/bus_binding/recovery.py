# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Persist a fallback episode until a healthy binding has replayed its outbox."""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from omnimarket.handlers.bus_binding import outbox
from omnimarket.models.bus_binding import (
    EnumBusBinding,
    EnumBusBindingState,
    ModelBusBindingDecision,
    ModelBusBindingSettlementPolicy,
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

if TYPE_CHECKING:
    from omnimarket.handlers.bus_binding import guard_refusal

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
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp = Path(handle.name)
            handle.write(json.dumps(record, sort_keys=True))
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


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
    result = create_handler_exception_emit().handle(
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


def _contract() -> dict[str, Any]:
    contract: dict[str, Any] = yaml.safe_load(
        resources.files("omnimarket.nodes.node_bus_binding_reducer")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return contract


def settlement_policy() -> ModelBusBindingSettlementPolicy:
    """The contract's exception settlement step; a contract that declares none keeps the per-command alarm."""
    raw = _contract().get("exception_settlement") or {"alarm_scope": "command"}
    return ModelBusBindingSettlementPolicy.model_validate(
        {
            **raw,
            "settle_on_healthy_read": tuple(raw.get("settle_on_healthy_read") or ()),
        }
    )


def alarm_once_per_episode(state_dir: Path, decision: ModelBusBindingDecision) -> str:
    """Emit the episode's alarm once; while the episode stays open, name that row and emit nothing.

    A row closed while its episode is still open (the orchestrator drained it) is not reopened by the next
    degraded command, so one outage counts once in the class's occurrences (OMN-21959).
    """
    record = read_record(state_dir, decision.lane)
    episode = record.get("outage_started_at")
    if episode and record.get("alarm_episode") == episode and record.get("alarm_id"):
        return f"held:{record['alarm_id']}"
    alarm = raise_alarm(state_dir, decision)
    status, _, row_id = alarm.partition(":")
    if episode and row_id and status in ("appended", "repeat"):
        record.update(alarm_id=row_id, alarm_episode=episode)
        _write_record(state_dir, decision.lane, record)
    return alarm


def escalation_bound_seconds() -> float:
    contract = _contract()
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


def _refusal_evidence(body: dict[str, Any]) -> str:
    """What the writer needs to see: the host's words, the exit code, the host, and which rows were lost."""
    refusal = body.get("refusal") or {}
    message = " ".join(str(refusal.get("message", "")).split())[:300] or "unreadable"
    kinds: list[str] = []
    stamps: list[str] = []
    for line in str(body.get("rows", "")).splitlines():
        cells = [cell.strip() for cell in line.split(" | ", 2)]
        if len(cells) >= 2 and cells[0][:2] == "20":
            stamps.append(cells[0])
            kinds.append(cells[1])
    return (
        f"ledger host {refusal.get('ledger_host', 'unknown')} refused the queued append "
        f"(exit_code={refusal.get('exit_code', 'unknown')}): {message}; "
        f"rows lost: {','.join(kinds) or 'unknown'} stamped {','.join(stamps) or 'unknown'}"
    )


def report_refused_to_writers(
    state_dir: Path,
    observed_at: datetime,
    *,
    since_ns: int = 0,
    guard: guard_refusal.GuardPorts | None = None,
) -> int:
    """Emit one non-decision case per refused request nobody has been told about yet; return how many.

    The case is keyed to the writer's lane, the one told "queued", not to the bus lane. It does not wait
    for an outage episode to close: a refusal is reported by the replay that found it. A report is marked
    on the refused file, so a later replay or episode does not report it twice.

    With ``guard`` bound (OMN-21959), a ledger guard's refusal whose cause is already gone is settled in code
    first and not emitted at all; only a case the guard actor cannot settle is emitted. Each pass then settles
    the open guard-refusal cases of the queue whose cause has gone since, without emitting any of them again.
    """
    directory = outbox.outbox_dir(state_dir) / outbox.REFUSED_DIRNAME
    count = 0
    if not directory.is_dir():
        _settle_open_guard_cases(state_dir, observed_at, guard)
        return count
    from omnimarket.handlers.bus_binding.guard_refusal import settle_guard_case

    for path in sorted(directory.glob("*.json")):
        try:
            queued_ns = int(path.stem.split("_", 1)[0])
            request_id = outbox.request_id_of(path)
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, IndexError):
            continue
        if (
            queued_ns < since_ns
            or not isinstance(body, dict)
            or body.get("reported_at")
        ):
            continue
        lane = str(body.get("requested_by_lane") or "unknown")
        row = ModelExceptionRow(
            cls=REFUSED_CLASS,
            key=f"lane={lane} request={request_id}",
            source=SOURCE,
            evidence=_refusal_evidence(body),
            tried=f"replayed the outbox; kept the refused request at {path}",
            proposed_fix="correct the row and append it again; the refused request is final",
            decision=False,
        )
        body["reported_at"] = observed_at.isoformat()
        settled = (
            settle_guard_case(
                state_dir,
                dataclasses.asdict(row),
                guard,
                now=observed_at,
                emit_open=False,
            )
            if guard is not None
            else None
        )
        if settled is not None and settled.resolved:
            body["settled"] = settled.how
            _write_text_atomically(path, json.dumps(body, sort_keys=True))
            continue
        create_handler_exception_emit().handle(
            ModelExceptionEmitRequest(
                state_dir=state_dir / "exception-queue",
                row=row,
                now=observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        )
        _write_text_atomically(path, json.dumps(body, sort_keys=True))
        count += 1
    _settle_open_guard_cases(state_dir, observed_at, guard)
    return count


def _settle_open_guard_cases(
    state_dir: Path, observed_at: datetime, guard: guard_refusal.GuardPorts | None
) -> None:
    """Settle the open guard-refusal cases whose cause is gone; an unsettled one is left as it is."""
    if guard is None:
        return
    from omnimarket.handlers.bus_binding.guard_refusal import settle_guard_refusals

    settle_guard_refusals(state_dir, guard, now=observed_at, emit_open=False)


def _write_text_atomically(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _refused_in_episode(state_dir: Path, started: str) -> int:
    directory = outbox.outbox_dir(state_dir) / outbox.REFUSED_DIRNAME
    since_ns = int(datetime.fromisoformat(started).timestamp() * 1_000_000_000)
    count = 0
    for path in directory.glob("*.json") if directory.is_dir() else ():
        try:
            queued_ns = int(path.stem.split("_", 1)[0])
        except (ValueError, IndexError):
            continue
        # Requests are queued while degraded, so a file older than the episode belongs to an earlier one.
        count += queued_ns >= since_ns
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
    # the episode open: it is reported as its own case, by the replay that found it. A stopped drain or an
    # unreplayed file does.
    if record.get("replay_stopped"):
        return
    # Include unreadable request files: ignoring one would falsely claim complete replay.
    if any(outbox.outbox_dir(state_dir).glob("*.json")):
        return
    queue_dir = state_dir / "exception-queue"
    report_refused_to_writers(state_dir, decision.observed_at)
    refused = _refused_in_episode(state_dir, record["outage_started_at"])
    opened = create_handler_exception_queue().handle(
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
            create_handler_exception_resolve().handle(
                ModelExceptionResolveRequest(
                    queue_dir,
                    row["id"],
                    SOURCE,
                    how,
                    decision.observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
            )
    record.update(
        outage_started_at=None,
        replayed=0,
        duplicates=0,
        refused=0,
        replay_stopped=None,
        alarm_id=None,
        alarm_episode=None,
    )
    _write_record(state_dir, decision.lane, record)
