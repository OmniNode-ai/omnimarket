# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reading writer: the only writer of reading rows (plan section 12, Gates).

It reads ledger rows through the configured source, hands them to node_reading_compute, checks
the row key against the readings already on the ledger, and appends the reading row. It writes
reading rows only and never a gate, a stop or an admission: a reading informs, it refuses nothing.

* A window is runnable only after its cutoff (window end plus the close delay).
* The same row key with the same content is already written and is not written again.
* The same row key with different content is a replay failure: the earlier row stands, and a
  ``replay-failed`` row (NOT_WORKING) records the attempt.
* A conflicting duplicate in the evidence fails the window (outcome ``window-failed``).
* A source that cannot be read gives an UNKNOWN row (outcome ``source-unreadable``).
* A probe (``write=False``) computes everything and appends nothing.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.models.model_reading import (
    EnumReadingOutcome,
    EnumReadingVerdict,
    ModelLedgerEvidenceRow,
    ModelPieceReadingSpec,
    ModelReadingComputeRequest,
    ModelReadingComputeResult,
    ModelReadingPieceOutcome,
    ModelReadingSourceRows,
    ModelReadingWindowCommand,
    ModelReadingWindowResult,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    HandlerReadingCompute,
    reason_token,
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelReadingOverlay
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
    ProtocolReadingAppender,
)

COMPUTE_CONTRACT = (
    Path(__file__).resolve().parents[2] / "node_reading_compute" / "contract.yaml"
)
WRITER_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_ROW_TYPE = "STATUS"
_ROW_SOURCE_CELL = "src=reading"


def load_revision(path: Path = COMPUTE_CONTRACT) -> str:
    """The reading's revision: the compute node's declared version, never a literal here."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    version = contract["node_version"]
    return f"{version['major']}.{version['minor']}.{version['patch']}"


def load_topics(path: Path = WRITER_CONTRACT) -> dict[str, str]:
    """Topics by role from the writer's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def window_for(
    now: datetime, length_seconds: int, close_delay_seconds: int
) -> tuple[datetime, datetime, datetime]:
    """The latest closed window as (start, end, cutoff); ends are multiples of the length."""
    seconds = int((now - timedelta(seconds=close_delay_seconds)).timestamp())
    end = datetime.fromtimestamp(seconds - seconds % length_seconds, UTC)
    return (
        end - timedelta(seconds=length_seconds),
        end,
        end + timedelta(seconds=close_delay_seconds),
    )


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def build_reading_row(
    result: ModelReadingComputeResult,
    *,
    lane: str,
    actor: str,
    published_at: datetime,
    probed_at: datetime,
    max_age_seconds: int,
    attempt: str | None = None,
) -> str:
    """The reading row the floor draws: one STATUS row, every cell one token."""
    cells = [
        f"lane={lane}",
        _ROW_SOURCE_CELL,
        f"actor={actor}",
        "model=none",
        f"piece={result.piece}",
        f"name={reason_token(result.name)}",
        f"verdict={result.verdict.value}",
        f"outcome={result.outcome.value}",
        f"reason={','.join(result.reasons)}",
    ]
    if result.basis:
        cells.append(f"basis={reason_token(result.basis)}")
    cells += [
        f"window_start={stamp_text(result.window_start)}",
        f"window_end={stamp_text(result.window_end)}",
        f"probed_at={stamp_text(probed_at)}",
        f"published_at={stamp_text(published_at)}",
        f"max_age={max_age_seconds}",
        f"key={result.row_key}",
        f"content={attempt or result.content_hash}",
        f"order_hash={result.order_hash[:12]}",
        f"overlay_hash={result.overlay_hash[:12]}",
        f"revision={result.revision}",
        f"manifest={result.manifest_hash[:12]}",
        f"rows_read={result.rows_read}",
        _clean(f"injected fault: {result.injected_fault}"),
    ]
    return f"{stamp_text(published_at)} | {_ROW_TYPE} | " + " | ".join(cells)


class HandlerReadingWriter:
    """Run the readings of one closed window and append their rows."""

    def __init__(
        self,
        *,
        overlay: ModelReadingOverlay,
        source: ProtocolLedgerRowSource,
        appender: ProtocolReadingAppender,
        clock: Callable[[], datetime],
        compute: HandlerReadingCompute | None = None,
        revision: str | None = None,
    ) -> None:
        self._overlay = overlay
        self._source = source
        self._appender = appender
        self._clock = clock
        self._compute = compute or HandlerReadingCompute()
        self._revision = revision or load_revision()

    # -- the window -------------------------------------------------------------------------

    def handle(self, command: ModelReadingWindowCommand) -> ModelReadingWindowResult:
        now = self._clock().astimezone(UTC)
        length = self._overlay.window.length_seconds
        delay = self._overlay.window.close_delay_seconds
        start = command.window_start.astimezone(UTC)
        if int(start.timestamp()) % length != 0:
            return self._failed(command, "window-not-aligned")
        end = start + timedelta(seconds=length)
        cutoff = end + timedelta(seconds=delay)
        if now < cutoff:
            return self._failed(command, "window-open", start, end, cutoff)
        declared = {spec.piece for spec in self._overlay.pieces}
        unknown = sorted(set(command.pieces) - declared)
        if unknown:
            return self._failed(
                command, f"unknown-pieces:{unknown}", start, end, cutoff
            )
        wanted = command.pieces or tuple(self._overlay.order)
        outcomes = tuple(
            self._piece(
                self._overlay.spec(piece), start, end, cutoff, now, command.write
            )
            for piece in wanted
        )
        return ModelReadingWindowResult(
            correlation_id=command.correlation_id,
            status="completed",
            window_start=start,
            window_end=end,
            cutoff=cutoff,
            pieces=outcomes,
        )

    @staticmethod
    def _failed(
        command: ModelReadingWindowCommand,
        why: str,
        start: datetime | None = None,
        end: datetime | None = None,
        cutoff: datetime | None = None,
    ) -> ModelReadingWindowResult:
        return ModelReadingWindowResult(
            correlation_id=command.correlation_id,
            status="failed",
            window_start=start,
            window_end=end,
            cutoff=cutoff,
            failure=why,
        )

    # -- one piece --------------------------------------------------------------------------

    def _evidence(
        self, spec: ModelPieceReadingSpec, end: datetime
    ) -> tuple[ModelReadingSourceRows, ...]:
        gathered = []
        for source in spec.sources:
            read = self._source.read(
                ModelLedgerQuery(
                    since=end - timedelta(seconds=source.max_age_seconds),
                    until=end,
                    row_type=source.row_type,
                    lane=source.lane,
                    term=source.term,
                )
            )
            gathered.append(
                ModelReadingSourceRows(
                    source_id=source.source_id, rows=read.rows, error=read.error
                )
            )
        return tuple(gathered)

    def _existing(
        self, piece: int, window_start: datetime, window_end: datetime, now: datetime
    ) -> tuple[tuple[ModelLedgerEvidenceRow, ...], str | None]:
        publish = self._overlay.publish
        read = self._source.read(
            ModelLedgerQuery(
                since=window_end,
                until=now + timedelta(seconds=1),
                row_type=_ROW_TYPE,
                lane=publish.lane,
                term=publish.reading_term,
            )
        )
        if read.error is not None:
            return (), read.error
        mine = tuple(
            row
            for row in read.rows
            if row.cells.get("piece") == str(piece)
            and row.cells.get("window_start") == stamp_text(window_start)
            and row.cells.get("outcome")
            in (EnumReadingOutcome.READ.value, EnumReadingOutcome.WINDOW_FAILED.value)
        )
        return mine, None

    def _piece(
        self,
        spec: ModelPieceReadingSpec,
        start: datetime,
        end: datetime,
        cutoff: datetime,
        now: datetime,
        write: bool,
    ) -> ModelReadingPieceOutcome:
        result = self._compute.handle(
            ModelReadingComputeRequest(
                spec=spec,
                window_start=start,
                window_end=end,
                cutoff=cutoff,
                order_hash=self._overlay.order_hash(),
                overlay_hash=self._overlay.overlay_hash(spec.piece),
                revision=self._revision,
                sources=self._evidence(spec, end),
            )
        )

        def outcome(
            final: ModelReadingComputeResult, ledger: str, row: str | None = None
        ) -> ModelReadingPieceOutcome:
            return ModelReadingPieceOutcome(
                piece=final.piece,
                verdict=final.verdict,
                outcome=final.outcome,
                reasons=final.reasons,
                row_key=final.row_key,
                ledger=ledger,
                row=row,
            )

        if not write:
            return outcome(result, "not-written")
        attempt: str | None = None
        if result.outcome.is_computed:
            existing, error = self._existing(spec.piece, start, end, now)
            if error is not None:
                return outcome(
                    result, f"append-failed:replay-check-{reason_token(error)}"
                )
            same_key = [
                row for row in existing if row.cells.get("key") == result.row_key
            ]
            if same_key:
                if all(
                    row.cells.get("content") == result.content_hash for row in same_key
                ):
                    return outcome(result, "already-written")
                attempt = result.content_hash
                result = result.model_copy(
                    update={
                        "verdict": EnumReadingVerdict.NOT_WORKING,
                        "outcome": EnumReadingOutcome.REPLAY_FAILED,
                        "reasons": (
                            "replay-failed:key-reused-with-different-content",
                            f"attempt-content-{attempt[:12]}",
                        ),
                    }
                )
        row = build_reading_row(
            result,
            lane=self._overlay.publish.lane,
            actor=self._overlay.publish.actor,
            published_at=now,
            probed_at=now,
            max_age_seconds=self._overlay.publish.publish_max_age_seconds,
            attempt=attempt,
        )
        request_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "|".join(
                    (
                        result.row_key,
                        attempt or result.content_hash,
                        result.outcome.value,
                        "" if result.outcome.is_computed else stamp_text(now),
                    )
                ),
            )
        )
        appended = self._appender.append(row + "\n", request_id)
        if not appended.ok:
            why = f":{reason_token(appended.message)}" if appended.message else ""
            return outcome(result, f"append-failed:{appended.status}{why}", row)
        return outcome(
            result,
            "written" if appended.status == "accepted" else "already-written",
            row,
        )


def events_of(
    result: ModelReadingWindowResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a run produces, in order, all under its correlation id.

    A piece reading is ``reading_piece_written``. A piece whose reading is a failure of the
    reading itself (window failed, replay failed, source unreadable, append failed) is
    ``reading_piece_failed``. The run ends in ``reading_window_completed``, or in
    ``reading_window_failed`` when no window could be run.
    """
    if result.status == "failed":
        return [
            (
                topics["reading_window_failed"],
                {"correlation_id": result.correlation_id, "failure": result.failure},
            )
        ]
    failed = {id(piece) for piece in result.failed_pieces}
    events: list[tuple[str, dict[str, object]]] = []
    for piece in result.pieces:
        topic = (
            topics["reading_piece_failed"]
            if id(piece) in failed or piece.outcome is not EnumReadingOutcome.READ
            else topics["reading_piece_written"]
        )
        events.append(
            (
                topic,
                {
                    "correlation_id": result.correlation_id,
                    "piece": piece.piece,
                    "verdict": piece.verdict.value,
                    "outcome": piece.outcome.value,
                    "reasons": list(piece.reasons),
                    "row_key": piece.row_key,
                    "ledger": piece.ledger,
                },
            )
        )
    events.append(
        (
            topics["reading_window_completed"],
            {
                "correlation_id": result.correlation_id,
                "pieces": len(result.pieces),
                "failed": len(result.failed_pieces),
            },
        )
    )
    return events


def answer_command(
    payload: bytes, handler: HandlerReadingWriter, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a window command is answered with a typed ``reading_window_failed``
    event naming the failure, never dropped and never retried silently.
    """
    try:
        command = ModelReadingWindowCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [(topics["reading_window_failed"], json.dumps(failure).encode())]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(handler.handle(command), topics)
    ]
