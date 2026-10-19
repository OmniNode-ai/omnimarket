# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic checkpoint and result effects; no model used for filesystem relays."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from omnimarket.nodes.node_hourly_tick_effect.models.model_hourly_tick import (
    ModelHourlyTickRequest,
    ModelHourlyTickResult,
    ModelPreparedTick,
    ModelSweepRequest,
    ModelSweepResult,
    ModelTickState,
)

HELD_CLASS_ORDER = (
    "floor_refused",
    "truncated",
    "unsourced",
    "no_consumer",
    "transport",
    "timeout",
)
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
FIRE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def prepared_result_path(root: Path, fire_id: str) -> Path:
    """Where the prepare command keeps the result it printed, keyed by the fire."""
    if not FIRE_ID.fullmatch(fire_id):
        raise ValueError("hourly-tick: record needs a valid fire id")
    return root / ".onex_state/hourly-tick/fires" / f"{fire_id}.prepared.json"


def build_record_request(
    prepared: ModelHourlyTickResult, sweep_text: str, fire_id: str
) -> ModelHourlyTickRequest:
    """The record request, built from the prepare result and the sweep return, never retyped.

    State, tail, facts, task type and correlation id come from the prepared result; the sweep
    fields come from the sweep return. A sweep return is a JSON object with a boolean `failed`
    (the sweep sets it on every return), so a stray file is refused and never recorded as n/a
    counts. The record validator is not loosened: the request it gets here is exactly the
    prepared state.
    """
    if prepared.phase != "prepared" or prepared.sweep_request is None:
        raise ValueError("hourly-tick: the prepared result awaits no sweep")
    try:
        raw = json.loads(sweep_text)
    except json.JSONDecodeError as error:
        raise ValueError(f"hourly-tick: sweep return is not JSON: {error}") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("failed"), bool):
        raise ValueError(
            "hourly-tick: sweep return is not an object with a boolean failed"
        )
    return ModelHourlyTickRequest(
        operation="record",
        correlation_id=prepared.correlation_id,
        fire_id=fire_id,
        state=prepared.state,
        facts=prepared.sweep_request.facts,
        task_type=prepared.sweep_request.task_type,
        sweep=ModelSweepResult.model_validate(raw),
    )


def checkpoint_line(p: ModelTickState) -> str:
    return (
        f"- {p.now} 60m checkpoint: ledger rows since {p.prev}: {p.rows}; "
        f"TERMINAL {p.terminal} [{p.lanes}]; sweep since={p.sweep_since}"
    )


def tick_outcome(sweep: ModelSweepResult | None) -> dict[str, str | None]:
    if sweep is not None and sweep.failed:
        return {"phase": "degraded", "class": sweep.error_class or "unclassified"}
    if sweep is None or not (sweep.needing and sweep.accepted == 0):
        return {"phase": "completed", "class": None}
    counts = sweep.held_by_class or {}
    best = max(HELD_CLASS_ORDER, key=lambda c: counts.get(c, 0))
    return {
        "phase": "degraded",
        "class": best if counts.get(best, 0) > 0 else "unclassified",
    }


def completion_line(
    p: ModelTickState, sweep: ModelSweepResult | None, run_id: str
) -> str:
    outcome = tick_outcome(sweep)
    counts = (
        f"FAILED error_class={outcome['class']}"
        if sweep is not None and sweep.failed
        else ", ".join(
            f"{field} {value if value is not None else 'n/a'}"
            for field in ("collected", "needing", "accepted", "held")
            for value in [getattr(sweep, field) if sweep is not None else None]
        )
    )
    phase = f"phase={outcome['phase']}" + (
        f" class={outcome['class']}" if outcome["class"] else ""
    )
    drafts = sweep.linear_file if sweep is not None and sweep.linear_file else "none"
    return (
        f"- {p.now} hourly-tick run={run_id or 'unrecorded'}: checkpoint rows {p.rows} / "
        f"TERMINAL {p.terminal}; sweep since={p.sweep_since} {counts}, {phase}; drafts {drafts}; "
        "NOT POSTED (per-message operator approval required)"
    )


def result_payload(fire_id: str, sweep: ModelSweepResult | None) -> dict[str, object]:
    return {
        "fire_id": fire_id,
        "workflow": "hourly-tick",
        **tick_outcome(sweep),
        **{
            field: getattr(sweep, field) if sweep is not None else None
            for field in ("needing", "accepted", "held", "held_by_class")
        },
    }


def facts_from_tail(tail: list[str], limit: int) -> str:
    lines = [line for line in tail if line.strip()][-limit:]
    if not lines:
        raise ValueError("hourly-tick: no facts supplied and checkpoint tail is empty")
    return "\n".join(
        [
            "Ground-truth facts, derived from the most recent orchestration "
            "checkpoint lines, oldest first.",
            "Each line is a timestamped statement of what a lane reported "
            "or what the operator ruled.",
            "Cite only what appears below. Anything not here is not a fact you may state.",
            "",
            *[line if line.startswith("- ") else f"- {line}" for line in lines],
        ]
    )


def probe(root: Path, request: ModelHourlyTickRequest) -> ModelTickState:
    tail = (root / request.checkpoint_file).read_text().splitlines()
    checkpoints = [
        m.group(1)
        for line in tail
        if (m := re.match(r"^- ([0-9T:Z-]*) 60m checkpoint:", line))
    ]
    if not checkpoints:
        raise ValueError("hourly-tick: probe field PREV is not an ISO-Z timestamp")
    prev = checkpoints[-1]
    since = "".join((root / request.sweep_state_file).read_text().split())
    if not TS.fullmatch(since):
        raise ValueError(
            "hourly-tick: probe field SWEEP_SINCE is not an ISO-Z timestamp"
        )
    now = request.now or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    ledger = (root / request.ledger_file).read_text().splitlines()
    timestamped = [
        line for line in ledger if len(line) >= 20 and TS.fullmatch(line[:20])
    ]
    rows = [line for line in timestamped if prev < line[:20] <= now]
    terminal = [line for line in rows if "| TERMINAL |" in line]
    lanes: set[str] = set()
    for line in terminal:
        for field in line.split("|"):
            if match := re.match(r"^lane=[A-Za-z0-9_.:-]+", field.strip()):
                lanes.add(match.group())
                break
    return ModelTickState(
        now=now,
        prev=prev,
        sweep_since=since,
        rows=len(rows),
        terminal=len(terminal),
        lanes=",".join(sorted(lanes)),
        ledger_total=sum(
            bool(re.match(r"^\d{4}-\d{2}-\d{2}T", line)) for line in ledger
        ),
        tail=[line.strip() for line in tail[-request.tail_lines :] if line.strip()],
    )


def append_readback(path: Path, line: str) -> str:
    with path.open("a") as f:
        f.write(line + "\n")
    readback = path.read_text().splitlines()[-1]
    if readback != line:
        raise OSError("hourly-tick: append readback differs")
    return f"READBACK: {readback}"


def write_pointer(path: Path, value: str) -> str:
    path.write_text(value + "\n")
    return f"READBACK: {path.read_text().strip()}"


class HandlerHourlyTick:
    """Definition-B effect core, dispatched by the shared runtime adapter.

    prepare writes the checkpoint before returning the sweep input; record consumes
    that exact state and the sweep return, writing the completion and atomic fire
    result. The sweep pointer moves only when the window was read: a skipped sweep
    moves it in prepare, a sweep whose Linear read did not fail moves it in record,
    and a failed read leaves it so the next tick reads the hour again (OMN-20047).
    The unconverted sweep is invoked by the host caller.
    """

    async def handle(self, request: ModelHourlyTickRequest) -> ModelHourlyTickResult:
        env_root = os.environ.get("OMNI_HOME")
        root = request.registry_root or (Path(env_root) if env_root else None)
        if root is None:
            raise ValueError("hourly-tick: set OMNI_HOME")
        readbacks: dict[str, str | None] = dict.fromkeys(
            ("checkpoint", "pointer", "completion", "result")
        )
        snapshot_path = (
            root / ".onex_state/hourly-tick/prepared" / f"{request.correlation_id}.json"
        )
        if request.operation == "record":
            if not snapshot_path.is_file():
                raise ValueError(
                    "hourly-tick: no node-owned prepared state for this correlation"
                )
            prepared = ModelPreparedTick.model_validate_json(snapshot_path.read_text())
            if (
                request.state != prepared.state
                or request.fire_id != prepared.request.fire_id
            ):
                raise ValueError(
                    "hourly-tick: caller changed the prepared state or fire id"
                )
            sweep = request.sweep
            request = prepared.request.model_copy(
                update={"operation": "record", "sweep": sweep}
            )
            readbacks.update(prepared.readbacks)
            state = prepared.state
        else:
            state = probe(root, request)
        if state is None:
            raise ValueError("hourly-tick: record requires prepared state")
        facts = request.facts or facts_from_tail(state.tail, request.tail_lines)
        skipped = request.skip_sweep or request.dry_run
        if request.operation == "prepare" and not request.dry_run:
            readbacks["checkpoint"] = append_readback(
                root / request.checkpoint_file, checkpoint_line(state)
            )
            if skipped:
                readbacks["pointer"] = write_pointer(
                    root / request.sweep_state_file, state.now
                )
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_snapshot = snapshot_path.with_suffix(".json.tmp")
            temporary_snapshot.write_text(
                ModelPreparedTick(
                    request=request, state=state, readbacks=readbacks
                ).model_dump_json()
                + "\n"
            )
            temporary_snapshot.replace(snapshot_path)
        sweep_request = (
            ModelSweepRequest(
                since=state.sweep_since, facts=facts, task_type=request.task_type
            )
            if request.operation == "prepare" and not skipped
            else None
        )
        done = ""
        phase: Literal["prepared", "completed", "degraded"] = "prepared"
        outcome = tick_outcome(request.sweep)
        if request.operation == "record" or skipped:
            done = completion_line(
                state, request.sweep, request.run_id or request.fire_id
            )
            phase = "degraded" if outcome["phase"] == "degraded" else "completed"
            if not request.dry_run:
                if request.operation == "record":
                    pointer = root / request.sweep_state_file
                    readbacks["pointer"] = (
                        f"READBACK: {pointer.read_text().strip()}"
                        if request.sweep is not None and request.sweep.failed
                        else write_pointer(pointer, state.now)
                    )
                readbacks["completion"] = append_readback(
                    root / request.checkpoint_file, done
                )
                if request.fire_id:
                    directory = root / ".onex_state/morning-workflows/results"
                    directory.mkdir(parents=True, exist_ok=True)
                    path = directory / f"{request.fire_id}.json"
                    temporary = path.with_suffix(".json.tmp")
                    temporary.write_text(
                        json.dumps(
                            result_payload(request.fire_id, request.sweep),
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    temporary.replace(path)
                    readbacks["result"] = f"READBACK: {path.read_text().strip()}"
        return ModelHourlyTickResult(
            correlation_id=request.correlation_id,
            state=state,
            checkpoint_line=checkpoint_line(state),
            completion_line=done,
            window={"from": state.prev, "to": state.now},
            sweep_since=state.sweep_since,
            ledger_rows=state.rows,
            terminal_rows=state.terminal,
            terminal_lanes=state.lanes,
            ledger_total_control=state.ledger_total,
            facts_source="caller-supplied"
            if request.facts
            else f"derived from the last {request.tail_lines} checkpoint lines",
            dry_run=request.dry_run,
            sweep_skipped=skipped,
            sweep_request=sweep_request,
            sweep=request.sweep,
            phase=phase,
            degraded=phase == "degraded",
            held_class=outcome["class"],
            readbacks=readbacks,
        )
