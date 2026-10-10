# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B decisions of the hourly tick (OMN-20680).

The retired tick mixed these decisions with host file reads and writes. Here the caller
reads the checkpoint file, the sweep pointer and the ledger and passes their contents; the
handler returns what to write. Formats are byte-identical to the retired tick (60 captured
cases in tests/fixtures/hourly_tick_parity.json).
"""

from __future__ import annotations

import re
from typing import Literal

from omnibase_core.types import JsonType

from omnimarket.nodes.node_hourly_tick_decision_compute.models.model_hourly_tick_decision import (
    EnumHourlyTickDecisionKind,
    ModelHourlyTickDecisionRequest,
    ModelHourlyTickDecisionResult,
    ModelSweepRequest,
    ModelSweepResult,
    ModelTickState,
)

HELD_CLASS_ORDER = ("floor_refused", "truncated", "unsourced", "transport", "timeout")
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def checkpoint_line(p: ModelTickState) -> str:
    return (
        f"- {p.now} 60m checkpoint: ledger rows since {p.prev}: {p.rows}; "
        f"TERMINAL {p.terminal} [{p.lanes}]; sweep since={p.sweep_since}"
    )


def tick_outcome(sweep: ModelSweepResult | None) -> dict[str, str | None]:
    """Degraded when something needed a reply and nothing was accepted; ties follow HELD_CLASS_ORDER."""
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
    counts = ", ".join(
        f"{field} {value if value is not None else 'n/a'}"
        for field in ("collected", "needing", "accepted", "held")
        for value in [getattr(sweep, field) if sweep is not None else None]
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


def result_payload(fire_id: str, sweep: ModelSweepResult | None) -> dict[str, JsonType]:
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


def probe_window(
    *,
    now: str,
    checkpoint_text: str,
    sweep_pointer_text: str,
    ledger_text: str,
    tail_lines: int,
) -> ModelTickState:
    """Window after the previous checkpoint through now. The ledger row count is the positive control."""
    tail = checkpoint_text.splitlines()
    checkpoints = [
        m.group(1)
        for line in tail
        if (m := re.match(r"^- ([0-9T:Z-]*) 60m checkpoint:", line))
    ]
    if not checkpoints:
        raise ValueError("hourly-tick: probe field PREV is not an ISO-Z timestamp")
    prev = checkpoints[-1]
    since = "".join(sweep_pointer_text.split())
    if not TS.fullmatch(since):
        raise ValueError(
            "hourly-tick: probe field SWEEP_SINCE is not an ISO-Z timestamp"
        )
    ledger = ledger_text.splitlines()
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
    ledger_total = sum(bool(re.match(r"^\d{4}-\d{2}-\d{2}T", line)) for line in ledger)
    if ledger_total <= 0:
        raise ValueError(
            "hourly-tick: ledger positive control failed: no timestamped rows"
        )
    return ModelTickState(
        now=now,
        prev=prev,
        sweep_since=since,
        rows=len(rows),
        terminal=len(terminal),
        lanes=",".join(sorted(lanes)),
        ledger_total=ledger_total,
        tail=[line.strip() for line in tail[-tail_lines:] if line.strip()],
    )


class HandlerHourlyTickDecision:
    """Stateless compute: probe the window, or record the sweep outcome."""

    def handle(
        self, request: ModelHourlyTickDecisionRequest
    ) -> ModelHourlyTickDecisionResult:
        if request.kind is EnumHourlyTickDecisionKind.PROBE:
            assert request.now is not None
            assert request.checkpoint_text is not None
            assert request.sweep_pointer_text is not None
            assert request.ledger_text is not None
            state = probe_window(
                now=request.now,
                checkpoint_text=request.checkpoint_text,
                sweep_pointer_text=request.sweep_pointer_text,
                ledger_text=request.ledger_text,
                tail_lines=request.tail_lines,
            )
            facts = request.facts or facts_from_tail(state.tail, request.tail_lines)
            facts_source = (
                "caller-supplied"
                if request.facts
                else f"derived from the last {request.tail_lines} checkpoint lines"
            )
        else:
            assert request.state is not None
            state = request.state
            facts = request.facts
            facts_source = None
        skipped = (
            request.skip_sweep and request.kind is EnumHourlyTickDecisionKind.PROBE
        )
        probing = request.kind is EnumHourlyTickDecisionKind.PROBE
        sweep_request = (
            ModelSweepRequest(
                since=state.sweep_since, facts=facts, task_type=request.task_type
            )
            if probing and not skipped
            else None
        )
        outcome = tick_outcome(request.sweep)
        completing = not probing or skipped
        phase: Literal["prepared", "completed", "degraded"] = "prepared"
        if completing:
            phase = "degraded" if outcome["phase"] == "degraded" else "completed"
        return ModelHourlyTickDecisionResult(
            kind=request.kind,
            state=state,
            checkpoint_line=checkpoint_line(state),
            facts_source=facts_source,
            sweep_request=sweep_request,
            completion_line=(
                completion_line(state, request.sweep, request.run_id or request.fire_id)
                if completing
                else None
            ),
            phase=phase,
            degraded=phase == "degraded",
            held_class=outcome["class"] if completing else None,
            result_payload=(
                result_payload(request.fire_id, request.sweep)
                if completing and request.fire_id
                else None
            ),
        )
