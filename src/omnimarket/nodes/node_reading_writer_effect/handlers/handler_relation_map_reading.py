# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The relation map reading: the progress of mapping each cut relation to joins, as one reading row.

Piece 1's standing run owes the map of each relation of the cycle cut to the joins of the pieces that
own its endpoints. This handler turns the import relation report into one reading row the floor
reads like any other (a STATUS row of the reading source, one cell per token), named ``relation_map``.

* The verdict is WORKING when the report was computed: the map is information, so unmapped
  relations show in the ``reason`` cell as progress and findings, and flip nothing else.
* No report (the graph run could not be read) is an UNKNOWN row naming the cause, never a skip.
* The same window and content append once: the request id is derived from the key and content.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import ValidationError

from omnimarket.models.import_relations import (
    ModelDependencyGateResult,
    ModelImportRelationReport,
    ModelRelationMapReadingCommand,
    ModelRelationMapReadingResult,
    ReadingVerdict,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    reason_token,
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.handler_reading_writer import (
    ROW_SOURCE_CELL,
    ROW_TYPE,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelReadingPublish
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)

ROW_NAME = "relation_map"
_UNREAD_REASON = "graph-unread"


def reason_of(report: ModelImportRelationReport | None) -> str:
    """The progress cell: tracked relations mapped, findings and cycles, or the unread cause."""
    if report is None:
        return _UNREAD_REASON
    return ",".join(
        (
            f"mapped={report.tracked_mapped}/{report.tracked_total}",
            f"findings={len(report.findings)}",
            f"cycles={len(report.cycles)}",
        )
    )


def gate_cells(gate: ModelDependencyGateResult | None) -> list[str]:
    """The dependency gate's counts as row cells: current and frozen, the floor shows both."""
    if gate is None:
        return ["count=unread", "frozen=unread", "gate=unread"]
    return [
        f"count={gate.current_count}",
        f"frozen={gate.frozen_count}",
        f"subsystems={gate.current_subsystems}",
        f"frozen_subsystems={gate.frozen_subsystems}",
        f"gate={gate.state}",
        f"baseline={gate.baseline_sha256[:16]}",
    ]


def build_relation_map_row(
    command: ModelRelationMapReadingCommand,
    *,
    publish: ModelReadingPublish,
    published_at: datetime,
    probed_at: datetime,
) -> tuple[str, ReadingVerdict]:
    """The row and its verdict. Every cell is one token; the floor reads the cells it needs."""
    report = command.report
    verdict: ReadingVerdict = "UNKNOWN" if report is None else "WORKING"
    outcome = "unread" if report is None else "read"
    content = "none" if report is None else report.digest()[:16]
    key = f"piece{command.piece}-{ROW_NAME}-{stamp_text(command.window_start)}"
    cells = [
        f"lane={publish.lane}",
        ROW_SOURCE_CELL,
        f"actor={publish.actor}",
        "model=none",
        f"piece={command.piece}",
        f"name={ROW_NAME}",
        f"verdict={verdict}",
        f"outcome={outcome}",
        f"reason={reason_of(report)}",
        f"probed_at={stamp_text(probed_at)}",
        f"published_at={stamp_text(published_at)}",
        f"max_age={publish.publish_max_age_seconds}",
        f"key={reason_token(key)}",
        f"content={content}",
    ]
    cells.extend(gate_cells(command.gate))
    if report is not None:
        for finding in report.findings:
            cells.append(
                f"finding={reason_token(finding.relation.text())}:{finding.kind.value}"
            )
    return f"{stamp_text(published_at)} | {ROW_TYPE} | " + " | ".join(cells), verdict


class HandlerRelationMapReading:
    """Append the relation map's reading row through the configured appender."""

    def __init__(
        self,
        *,
        publish: ModelReadingPublish,
        appender: ProtocolReadingAppender,
        clock: Callable[[], datetime],
    ) -> None:
        self._publish = publish
        self._appender = appender
        self._clock = clock

    def handle(
        self, command: ModelRelationMapReadingCommand
    ) -> ModelRelationMapReadingResult:
        now = self._clock().astimezone(UTC)
        row, verdict = build_relation_map_row(
            command, publish=self._publish, published_at=now, probed_at=now
        )
        reason = reason_of(command.report)
        if not command.write:
            return ModelRelationMapReadingResult(
                correlation_id=command.correlation_id,
                status="not-written",
                verdict=verdict,
                reason=reason,
                row=row,
            )
        content = "none" if command.report is None else command.report.digest()
        if command.gate is not None:
            content += command.gate.model_dump_json()
        request_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "|".join(
                    (
                        ROW_NAME,
                        str(command.piece),
                        stamp_text(command.window_start),
                        content,
                    )
                ),
            )
        )
        appended = self._appender.append(row + "\n", request_id)
        if not appended.ok:
            return ModelRelationMapReadingResult(
                correlation_id=command.correlation_id,
                status="failed",
                verdict=verdict,
                reason=reason,
                row=row,
                failure=f"append-failed:{appended.status}",
            )
        return ModelRelationMapReadingResult(
            correlation_id=command.correlation_id,
            status="written" if appended.status == "accepted" else "already-written",
            verdict=verdict,
            reason=reason,
            row=row,
        )


def events_of_relation_map(
    result: ModelRelationMapReadingResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """One event under the command's correlation id: written (or already written), or failed."""
    failed = result.status == "failed"
    return [
        (
            topics["relation_map_failed" if failed else "relation_map_written"],
            {
                "correlation_id": result.correlation_id,
                "status": result.status,
                "verdict": result.verdict,
                "reason": result.reason,
                "failure": result.failure,
            },
        )
    ]


def answer_relation_map_command(
    payload: bytes, handler: HandlerRelationMapReading, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, its event out. A payload that is no command is a typed failure."""
    try:
        command = ModelRelationMapReadingCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {
            "correlation_id": "unknown",
            "status": "failed",
            "verdict": "UNKNOWN",
            "reason": _UNREAD_REASON,
            "failure": f"bad-payload:{fields}",
        }
        return [(topics["relation_map_failed"], json.dumps(failure).encode())]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of_relation_map(handler.handle(command), topics)
    ]
