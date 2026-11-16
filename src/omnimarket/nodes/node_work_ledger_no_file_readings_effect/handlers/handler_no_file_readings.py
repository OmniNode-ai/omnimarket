# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seven readings that prove no file ledger (plan section 4.3), run as one node.

``handle(ModelNoFileRequest) -> ModelNoFileResult``. Each reading reads its facts through the
injected probe port and is judged by a pure function; a probe that cannot run gives UNKNOWN, which
counts as NOT_WORKING for ``all_working``. A NOT_WORKING reading is a reading and not a failure of
the run: the run is one emission chain (piece 6) that ends in success when all seven were read (and
written, on a writing run) and in a typed failure when the run itself broke: no appender for a
writing run, or an append that did not land. Each reading carries the break that must turn it red.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import ValidationError

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)
from omnimarket.nodes.node_work_ledger_no_file_readings_effect.handlers import judges
from omnimarket.nodes.node_work_ledger_no_file_readings_effect.models import (
    ModelNoFileOverlay,
    ModelNoFileReading,
    ModelNoFileRequest,
    ModelNoFileResult,
)
from omnimarket.nodes.node_work_ledger_no_file_readings_effect.protocols import (
    ProtocolNoFileFacts,
)

NODE_NAME = "node_work_ledger_no_file_readings_effect"
PIECE = 6
_ROW_TYPE = "STATUS"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ").strip() or "none"


class HandlerNoFileReadings:
    def __init__(
        self,
        overlay: ModelNoFileOverlay,
        facts: ProtocolNoFileFacts,
        sink: ProtocolEmissionSink,
        appender: ProtocolReadingAppender | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        backend: str = "real",
    ) -> None:
        self._overlay = overlay
        self._facts = facts
        self._sink = sink
        self._appender = appender
        self._clock = clock
        self._backend = backend

    def handle(self, request: ModelNoFileRequest) -> ModelNoFileResult:
        chain = ChainEmitter(
            self._sink,
            node=NODE_NAME,
            piece=PIECE,
            origin="probe",
            clock=self._clock,
            correlation_id=request.correlation_id,
            backend="in_memory" if self._backend == "in_memory" else "real",
        )
        chain.start()
        if request.write and self._appender is None:
            return self._failed(chain, request, (), "no-appender-for-a-writing-run")
        now = self._clock().astimezone(UTC)
        readings = [self._reading(spec.number) for spec in self._overlay.readings]
        if request.write:
            written: list[ModelNoFileReading] = []
            for reading in readings:
                reading = self._write(reading, now, request.correlation_id)
                written.append(reading)
                if reading.ledger.startswith("append-failed"):
                    return self._failed(
                        chain,
                        request,
                        tuple(written),
                        "append-failed",
                        f"reading {reading.number}: {reading.ledger}",
                    )
            readings = written
        working = sum(r.verdict is EnumReadingVerdict.WORKING for r in readings)
        chain.success({"readings": len(readings), "working": working})
        return ModelNoFileResult(
            correlation_id=request.correlation_id,
            status="completed",
            readings=tuple(readings),
            all_working=working == len(readings),
        )

    def _failed(
        self,
        chain: ChainEmitter,
        request: ModelNoFileRequest,
        readings: tuple[ModelNoFileReading, ...],
        code: str,
        detail: str = "",
    ) -> ModelNoFileResult:
        chain.failure(code, detail)
        return ModelNoFileResult(
            correlation_id=request.correlation_id,
            status="failed",
            readings=readings,
            failure_code=code,
            failure_detail=detail[:300],
        )

    def _reading(self, number: int) -> ModelNoFileReading:
        spec = next(s for s in self._overlay.readings if s.number == number)
        overlay = self._overlay
        facts = self._facts
        try:
            if number == 1:
                reasons, judged = judges.judge_file_probe(facts.file_probe())
            elif number == 2:
                reasons, judged = judges.judge_census(facts.census(), overlay)
            elif number == 3:
                reasons, judged = judges.judge_absence(facts.absence(), overlay)
            elif number == 4:
                reasons, judged = judges.judge_rebuild(facts.rebuild())
            elif number == 5:
                reasons, judged = judges.judge_restore(facts.restore())
            elif number == 6:
                reasons, judged = judges.judge_publish(facts.publish(), overlay)
            else:
                reasons, judged = judges.judge_receipt(facts.receipt(), overlay)
        except (OSError, ValueError, ValidationError) as exc:
            return ModelNoFileReading(
                number=number,
                name=spec.name,
                verdict=EnumReadingVerdict.UNKNOWN,
                outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
                reasons=(f"probe-failed-{judges.reason_token(type(exc).__name__)}",),
                injected_fault=spec.injected_fault,
            )
        if not judged:
            return ModelNoFileReading(
                number=number,
                name=spec.name,
                verdict=EnumReadingVerdict.UNKNOWN,
                outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
                reasons=tuple(reasons),
                injected_fault=spec.injected_fault,
            )
        return ModelNoFileReading(
            number=number,
            name=spec.name,
            verdict=EnumReadingVerdict.NOT_WORKING
            if reasons
            else EnumReadingVerdict.WORKING,
            outcome=EnumReadingOutcome.READ,
            reasons=tuple(reasons) or (f"{spec.name}-holds",),
            injected_fault=spec.injected_fault,
        )

    def _write(
        self, reading: ModelNoFileReading, now: datetime, correlation_id: str
    ) -> ModelNoFileReading:
        assert self._appender is not None
        o = self._overlay
        overlay_hash = _sha(o.model_dump_json())[:12]
        # The key rides on the request's correlation id, not the clock: a redelivery of the same request
        # in another second must reach the appender under the same request id.
        key = _sha(f"{o.lane}|{reading.number}|{correlation_id}|{overlay_hash}")[:32]
        content = _sha(
            json.dumps(
                [reading.verdict.value, reading.outcome.value, list(reading.reasons)]
            )
        )[:32]
        cells = [
            f"lane={o.lane}",
            f"src={o.row_source}",
            f"actor={o.actor}",
            "model=none",
            f"reading={reading.number}",
            f"name={reading.name}",
            f"verdict={reading.verdict.value}",
            f"outcome={reading.outcome.value}",
            f"reason={','.join(reading.reasons)}",
            f"probed_at={_stamp(now)}",
            f"published_at={_stamp(now)}",
            f"max_age={o.max_age_seconds}",
            f"key={key}",
            f"content={content}",
            f"overlay_hash={overlay_hash}",
            _cell(f"injected fault: {reading.injected_fault}"),
        ]
        row = f"{_stamp(now)} | {_ROW_TYPE} | " + " | ".join(cells)
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{key}|{content}"))
        outcome = self._appender.append(row + "\n", request_id)
        ledger = (
            f"append-failed:{outcome.status}"
            if not outcome.ok
            else ("written" if outcome.status == "accepted" else "already-written")
        )
        return reading.model_copy(update={"row": row, "ledger": ledger})
