# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Detect in-force ledger HOLDs; RELEASE never substitutes for a stop's readback.

The queue owns both persistence and the role checks. Its durable emission key
prevents a reread from reopening a stop that the reducer already cleared.
"""

from __future__ import annotations

import fcntl
from datetime import datetime

from omnimarket.handlers.ledger_row_grammar import Row, hold_release_state, parse_row
from omnimarket.handlers.rules_stop_gate import load_andon_stop_contract
from omnimarket.handlers.work_ledger_text import split_ledger_rows
from omnimarket.models.model_andon_ledger_holds import (
    ModelAndonLedgerHoldsRequest,
    ModelAndonLedgerHoldsResult,
)
from omnimarket.models.stop_gate import OPEN_ROLE_KEY, EnumStopEventKind, ModelStopEvent
from omnimarket.nodes.node_exception_queue_effect.handler import (
    HandlerExceptionEmit,
    HandlerExceptionQueue,
    HandlerStopEvent,
)
from omnimarket.nodes.node_exception_queue_effect.models import (
    ModelExceptionEmitRequest,
    ModelExceptionQueueRequest,
    ModelExceptionRow,
    ModelStopEventRequest,
)


def _readback(row: Row) -> str:
    return (
        row.value("readback_command")
        or row.value("readback")
        or row.text.partition("Readback:")[2].strip()
    )


def _rows(text: str, now: datetime) -> list[Row]:
    rows: list[Row] = []
    for raw in split_ledger_rows(text):
        row = parse_row(raw.split("\n", 1)[0], types=frozenset({"HOLD", "RELEASE"}))
        if row is not None and datetime.fromisoformat(row.stamp) <= now:
            row.text = raw
            rows.append(row)
    return rows


class HandlerAndonLedgerHolds:
    """Reconcile a ledger snapshot before the queue supplies stops to dispatchers."""

    def handle(
        self, request: ModelAndonLedgerHoldsRequest
    ) -> ModelAndonLedgerHoldsResult:
        # Serialize the emit/lease pair; the queue takes its own lock per write.
        request.state_dir.mkdir(parents=True, exist_ok=True)
        with (request.state_dir / "andon-ledger.lock").open(
            "a", encoding="utf-8"
        ) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                return self._reconcile(request)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _reconcile(
        self, request: ModelAndonLedgerHoldsRequest
    ) -> ModelAndonLedgerHoldsResult:
        text = request.ledger_path.read_text(encoding="utf-8")
        rows = _rows(text, request.now)
        releases = [row for row in rows if row.row_type == "RELEASE"]
        fsm, config = load_andon_stop_contract()
        emitter = HandlerExceptionEmit(fsm=fsm, config=config)
        events = HandlerStopEvent(fsm=fsm, config=config)
        ids: list[str] = []
        reports: list[str] = []
        for row in rows:
            if row.row_type != "HOLD" or not row.value("class"):
                continue
            hold_id = row.token("id")
            cls, key, swarm = row.token("class"), row.token("key"), row.token("swarm")
            if not all((hold_id, cls, key, swarm)):
                reports.append(
                    f"hold={hold_id or row.stamp} invalid_hold: requires id, class, key, swarm"
                )
                continue
            if hold_release_state(row, releases)[0]:
                continue
            until = row.value("until")
            if until:
                try:
                    expiry = datetime.fromisoformat(until)
                    if expiry.tzinfo is None:
                        raise ValueError("until must be timezone-aware")
                except ValueError:
                    reports.append(f"hold={hold_id} invalid_until:{until}")
                    continue
                if expiry <= request.now:
                    continue
            assert cls is not None
            assert key is not None
            assert swarm is not None
            if config.stop_class(cls) is None:
                reports.append(f"hold={hold_id} class_not_registered:{cls}")
                continue
            emitted = emitter.handle(
                ModelExceptionEmitRequest(
                    state_dir=request.state_dir,
                    row=ModelExceptionRow(
                        cls=cls,
                        key=key,
                        source=f"ledger-hold:{hold_id}",
                        evidence=row.text,
                        tried="read an in-force ledger HOLD",
                        proposed_fix=f"swarm lane {swarm} repairs the cause; the retained readback clears the stop",
                        readback_command=_readback(row),
                    ),
                    now=row.stamp,
                    role=config.writer_roles[OPEN_ROLE_KEY].value,
                    idempotency_key=f"ledger-hold:{hold_id}",
                )
            )
            if emitted.status == "refused":
                reports.append(f"hold={hold_id} emit_refused:{emitted.detail}")
                continue
            ids.append(emitted.id)
            # Retry a lease after a crash between emit and grant, but never renew
            # a granted lease or revive a closed stop while rereading the HOLD.
            opened = HandlerExceptionQueue().handle(
                ModelExceptionQueueRequest(request.state_dir, "orchestrator")
            )
            stop = next(
                (r.get("stop") for r in opened.rows if r["id"] == emitted.id), None
            )
            if (
                stop is not None
                and stop["state"] == fsm.initial_state
                and not stop.get("swarm_lane")
            ):
                granted = events.handle(
                    ModelStopEventRequest(
                        state_dir=request.state_dir,
                        event=ModelStopEvent(
                            stop_id=emitted.id,
                            kind=EnumStopEventKind.LEASE_GRANTED,
                            role=config.writer_roles[
                                EnumStopEventKind.LEASE_GRANTED.value
                            ].value,
                            at=request.now,
                            lane_id=swarm,
                        ),
                        now=request.now.isoformat(),
                    )
                )
                if granted.status == "refused":
                    reports.append(f"hold={hold_id} lease_refused:{granted.reason}")
        return ModelAndonLedgerHoldsResult(stop_ids=tuple(ids), reports=tuple(reports))
