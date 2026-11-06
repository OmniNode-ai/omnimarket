# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Demand each declared seam's command, read whether its result came by the deadline, append one row.

Piece 3 of the base-first plan: a per-seam reading with a deadline. It writes seam reading rows only,
never a gate, a stop or an admission. A seam is WORKING when its correlated success result arrived
by its deadline; no result by the deadline, an error answer, or a bus that cannot be read is
NOT_WORKING with the reason named. The run is WORKING only when every seam is. A run with a seam
that did not cross ends in the typed failure event and its row reads NOT_WORKING. A run with an
injected fault is a drill, and its row carries ``drill=yes`` so that no operational reading reads it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    reason_token,
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.models.model_seam_deadline import (
    EnumSeamCrossing,
    EnumSeamFault,
    ModelSeamDeadlineCommand,
    ModelSeamDeadlineResult,
    ModelSeamDemand,
    ModelSeamReading,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.overlay import (
    ModelSeamDeadlineOverlay,
    ModelSeamSpec,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.protocols import ProtocolSeamBus

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_ROW_TYPE = "STATUS"
_ROW_SOURCE_CELL = "src=seam-deadline"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def _cell(text: str) -> str:
    """A cell value: no pipe, no space."""
    return text.replace("|", "/").replace(" ", "_")


class HandlerSeamDeadline:
    """Run the overlay's seams over the injected bus."""

    def __init__(
        self,
        *,
        overlay: ModelSeamDeadlineOverlay,
        host: str,
        bus: ProtocolSeamBus,
        appender: ProtocolReadingAppender | None,
        clock: Callable[[], datetime],
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._overlay = overlay
        self._host = host
        self._bus = bus
        self._appender = appender
        self._clock = clock
        self._new_id = new_id

    def _demand(self, seam: ModelSeamSpec, fault: EnumSeamFault) -> ModelSeamDemand:
        return ModelSeamDemand(
            seam=seam.name,
            command_topic=seam.command_topic,
            result_topic=seam.result_topic,
            consumer_group_prefix=seam.consumer_group_prefix,
            correlation_id=self._new_id(),
            payload=seam.fault_request
            if fault is EnumSeamFault.UNANSWERABLE_COMMAND
            else seam.request,
            error_keys=seam.error_keys,
            deadline_seconds=0.0
            if fault is EnumSeamFault.DEADLINE_ZERO
            else seam.deadline_seconds,
        )

    def handle(self, command: ModelSeamDeadlineCommand) -> ModelSeamDeadlineResult:
        now = self._clock().astimezone(UTC)
        if command.write and self._appender is None:
            return self._refused(command, "no-appender-for-a-writing-run")
        unknown = [name for name in command.seams if self._overlay.seam(name) is None]
        if unknown:
            return self._refused(
                command, f"seam-not-declared:{reason_token(unknown[0])}"
            )
        chosen = [
            seam
            for seam in self._overlay.seams
            if not command.seams or seam.name in command.seams
        ]
        readings = []
        for seam in chosen:
            demand = self._demand(seam, command.fault)
            answer = self._bus.demand(demand)
            readings.append(
                ModelSeamReading(
                    seam=seam.name,
                    verdict=EnumReadingVerdict.WORKING
                    if answer.crossing is EnumSeamCrossing.RESULT
                    else EnumReadingVerdict.NOT_WORKING,
                    crossing=answer.crossing,
                    elapsed_ms=answer.elapsed_ms,
                    deadline_seconds=demand.deadline_seconds,
                    correlation_id=demand.correlation_id,
                    detail=answer.detail,
                )
            )
        failed = [r for r in readings if r.verdict is not EnumReadingVerdict.WORKING]
        unreadable = (
            all(r.crossing is EnumSeamCrossing.BUS_UNREADABLE for r in failed)
            and failed
        )
        reasons = (
            ("ok",)
            if not failed
            else tuple(f"{r.seam}:{r.crossing.value}" for r in failed)
        )
        result = ModelSeamDeadlineResult(
            correlation_id=command.correlation_id,
            status="failed" if failed else "completed",
            verdict=EnumReadingVerdict.NOT_WORKING
            if failed
            else EnumReadingVerdict.WORKING,
            outcome=EnumReadingOutcome.SOURCE_UNREADABLE
            if unreadable
            else EnumReadingOutcome.READ,
            reasons=reasons,
            fault=command.fault,
            seams=tuple(readings),
            failure=f"seam-not-crossed:{','.join(reasons)}" if failed else None,
        )
        return self._publish(command, now, result)

    def _refused(
        self, command: ModelSeamDeadlineCommand, why: str
    ) -> ModelSeamDeadlineResult:
        return ModelSeamDeadlineResult(
            correlation_id=command.correlation_id,
            status="failed",
            verdict=EnumReadingVerdict.UNKNOWN,
            outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
            reasons=(why,),
            fault=command.fault,
            failure=why,
        )

    def _publish(
        self,
        command: ModelSeamDeadlineCommand,
        now: datetime,
        result: ModelSeamDeadlineResult,
    ) -> ModelSeamDeadlineResult:
        overlay_hash = self._overlay.overlay_hash()
        seam_names = ",".join(r.seam for r in result.seams)
        key = _sha(
            "|".join(
                (
                    self._host,
                    stamp_text(now),
                    overlay_hash,
                    seam_names,
                    command.fault.value,
                )
            )
        )[:32]
        content = _sha(
            json.dumps(
                [result.verdict.value, result.outcome.value, list(result.reasons)],
                sort_keys=True,
            )
        )[:32]
        if not command.write:
            return result.model_copy(update={"row_key": key, "ledger": "not-written"})
        assert self._appender is not None
        publish = self._overlay.publish
        working = sum(r.verdict is EnumReadingVerdict.WORKING for r in result.seams)
        cells = [
            f"lane={publish.lane}",
            _ROW_SOURCE_CELL,
            f"actor={publish.actor}",
            "model=none",
            f"host={self._host}",
            f"drill={'no' if command.fault is EnumSeamFault.NONE else 'yes'}",
            f"fault={command.fault.value}",
            f"verdict={result.verdict.value}",
            f"outcome={result.outcome.value}",
            f"reason={_cell(','.join(result.reasons))}",
            f"seams={len(result.seams)}",
            f"working={working}",
            *(
                f"seam_{r.seam}={r.verdict.value}:{r.crossing.value}:"
                f"{'none' if r.elapsed_ms is None else r.elapsed_ms}ms/"
                f"{int(r.deadline_seconds * 1000)}ms"
                for r in result.seams
            ),
            f"history_days={self._overlay.history_days}",
            f"probed_at={stamp_text(now)}",
            f"published_at={stamp_text(now)}",
            f"max_age={publish.publish_max_age_seconds}",
            f"key={key}",
            f"content={content}",
            f"overlay_hash={overlay_hash[:12]}",
            f"correlation={_cell(command.correlation_id)}",
            _clean(f"injected fault: {self._overlay.injected_fault}"),
        ]
        row = f"{stamp_text(now)} | {_ROW_TYPE} | " + " | ".join(cells)
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{key}|{content}"))
        appended = self._appender.append(row + "\n", request_id)
        if not appended.ok:
            why = f"append-failed:{appended.status}"
            return result.model_copy(
                update={
                    "status": "failed",
                    "row_key": key,
                    "ledger": why,
                    "row": row,
                    "failure": result.failure
                    or f"{why}:{reason_token(appended.message or 'no-message')}",
                }
            )
        return result.model_copy(
            update={
                "row_key": key,
                "ledger": "written"
                if appended.status == "accepted"
                else "already-written",
                "row": row,
            }
        )


def events_of(
    result: ModelSeamDeadlineResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a run produces, in order, all under its correlation id."""
    events: list[tuple[str, dict[str, object]]] = [
        (
            topics["seam_deadline_run_started"],
            {"correlation_id": result.correlation_id, "fault": result.fault.value},
        )
    ]
    events.extend(
        (
            topics["seam_crossing_read"],
            {
                "correlation_id": result.correlation_id,
                "seam": reading.seam,
                "verdict": reading.verdict.value,
                "crossing": reading.crossing.value,
                "elapsed_ms": reading.elapsed_ms,
                "deadline_seconds": reading.deadline_seconds,
                "demand_correlation_id": reading.correlation_id,
            },
        )
        for reading in result.seams
    )
    final: dict[str, object] = {
        "correlation_id": result.correlation_id,
        "verdict": result.verdict.value,
        "outcome": result.outcome.value,
        "reasons": list(result.reasons),
        "row_key": result.row_key,
        "ledger": result.ledger,
    }
    if result.status == "failed":
        final["failure"] = result.failure
        events.append((topics["seam_deadline_run_failed"], final))
    else:
        events.append((topics["seam_deadline_run_completed"], final))
    return events


def answer_command(
    payload: bytes, handler: HandlerSeamDeadline, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a command is answered with a typed failure event, never dropped.
    """
    try:
        command = ModelSeamDeadlineCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [(topics["seam_deadline_run_failed"], json.dumps(failure).encode())]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(handler.handle(command), topics)
    ]


__all__ = ["HandlerSeamDeadline", "answer_command", "events_of", "load_topics"]
