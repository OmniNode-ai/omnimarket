# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J4, overlays to secrets (OMN-22912, plan section 4.1): a node resolves a key by overlay from the
store, and a secret scan finds no key value in its code, its log or the ledger row it writes.

One run emits two chains under one correlation id, through the floor's emission contract, so the
floor's own receipt fold judges the join:

* Piece 4, the consuming node: start, resolve its key through the overlay's handle, use it, log and
  write by name only, success. When the scan finds the value on a surface, or the key does not
  resolve, the chain ends in a typed failure.
* Piece 5, the scan: start after the consumer's success, success on a clean scan that saw its
  planted dummy. When the consumer failed, the scan answers with a typed ``upstream_`` refusal and
  never starts, the join's error shape.

The join reads WORKING only on the golden shape. A row that carries the value is never appended:
the row is rebuilt from the join's own cells. A resolved value is used in process and never put in
a log line, a row, an event or a result by this handler.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.models.floor_17a import ModelNodeEmission
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.models.model_secret_join_j4 import (
    EnumJ4Surface,
    ModelJ4Scan,
    ModelJoinJ4Command,
    ModelJoinJ4Result,
)
from omnimarket.models.model_secret_reading import (
    EnumSecretProbeOutcome,
    ModelSecretHandle,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    UPSTREAM_PREFIX,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)
from omnimarket.nodes.node_secret_reading_effect.overlay import (
    ModelSecretReadingOverlay,
)
from omnimarket.nodes.node_secret_reading_effect.overlay_join_j4 import (
    ModelJoinJ4Overlay,
)
from omnimarket.nodes.node_secret_reading_effect.protocols import (
    ProtocolExposureSurfaces,
    ProtocolSecretProbeStore,
)

_ROW_TYPE = "STATUS"
_ROW_SOURCE_CELL = "src=secret-join-j4"
JOIN_ID = "J4"
FAILURE_EXPOSED = "secret_value_exposed"
FAILURE_SCAN_BLIND = "scan_blind"
FAILURE_SCAN_UNREADABLE = "surface_unreadable"
FAILURE_KEY_PREFIX = "consumer_key_"


class ProtocolJ4Consumer(Protocol):
    """The consuming node's use of its key: it gets the value in process, logs through ``log`` and
    returns the cells of the ledger row it wants written. A value in either breaks the join."""

    def run(
        self, *, handle: ModelSecretHandle, value: str, log: Callable[[str], None]
    ) -> str: ...


class OverlayKeyConsumer:
    """The consumer as a node writes it: the key is named, never shown."""

    def run(
        self, *, handle: ModelSecretHandle, value: str, log: Callable[[str], None]
    ) -> str:
        log(f"resolved {handle.key} for {handle.consumer} from {handle.folder}")
        return f"consumer={handle.consumer} | key={handle.key} | folder={handle.folder}"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


class HandlerSecretJoinJ4:
    """``handle(ModelJoinJ4Command) -> ModelJoinJ4Result``."""

    def __init__(
        self,
        *,
        reading: ModelSecretReadingOverlay,
        join: ModelJoinJ4Overlay,
        store: ProtocolSecretProbeStore,
        surfaces: ProtocolExposureSurfaces,
        sink: ProtocolEmissionSink,
        appender: ProtocolReadingAppender | None,
        consumer: ProtocolJ4Consumer,
        log: Callable[[str], None],
        clock: Callable[[], datetime],
        host: str,
    ) -> None:
        self._reading = reading
        self._join = join
        self._store = store
        self._surfaces = surfaces
        self._sink = sink
        self._appender = appender
        self._consumer = consumer
        self._log = log
        self._clock = clock
        self._host = host

    def handle(self, command: ModelJoinJ4Command) -> ModelJoinJ4Result:
        handle = self._reading.consumer(self._join.consumer)
        if handle is None:
            return self._failed(command, f"consumer-not-declared:{self._join.consumer}")
        if command.write and self._appender is None:
            return self._failed(command, "no-appender-for-a-writing-run")
        backend = "in_memory" if self._reading.store.secret_store is None else "real"
        consumer_chain = ChainEmitter(
            self._sink,
            node=self._join.consumer_node,
            piece=self._join.consumer_piece,
            clock=self._clock,
            correlation_id=command.correlation_id,
            backend=backend,
            drill=self._join.drill,
            origin="drill" if self._join.drill else "probe",
        )
        consumer_chain.start()
        resolved = self._store.resolve(handle)
        if resolved.value is None:
            code = FAILURE_KEY_PREFIX + resolved.outcome.value
            return self._refused(
                command, consumer_chain, code, resolved.outcome, backend
            )

        log_lines: list[str] = []

        def log(line: str) -> None:
            log_lines.append(line)
            self._log(line)

        value = resolved.value.get_secret_value()
        consumer_cells = self._consumer.run(handle=handle, value=value, log=log)
        scan = self._scan(value, log_lines, consumer_cells)
        del value
        now = self._clock().astimezone(UTC)
        if scan.exposed:
            return self._refused(
                command,
                consumer_chain,
                FAILURE_EXPOSED,
                resolved.outcome,
                backend,
                scan=scan,
                now=now,
            )
        consumer_chain.success()
        scan_chain = ChainEmitter(
            self._sink,
            node=self._join.scan_node,
            piece=self._join.scan_piece,
            origin="drill" if self._join.drill else "probe",
            clock=self._clock,
            correlation_id=command.correlation_id,
            backend=backend,
            drill=self._join.drill,
        )
        scan_chain.start()
        reasons: list[str] = []
        if not scan.dummy_found:
            reasons.append("planted-dummy-missed")
        if scan.unreadable:
            reasons.append(f"surface-unreadable-{len(scan.unreadable)}")
        if reasons:
            scan_chain.failure(
                FAILURE_SCAN_BLIND if not scan.dummy_found else FAILURE_SCAN_UNREADABLE,
                ",".join(reasons),
            )
            verdict = EnumReadingVerdict.NOT_WORKING
            scan_state = "failure"
        else:
            scan_chain.success(
                {
                    "code_files": scan.code_files,
                    "log_lines": scan.log_lines,
                    "ledger_rows": scan.ledger_rows,
                }
            )
            verdict = EnumReadingVerdict.WORKING
            scan_state = "success"
        unread = bool(scan.unreadable)
        result = ModelJoinJ4Result(
            correlation_id=command.correlation_id,
            status="completed",
            verdict=verdict,
            outcome=EnumReadingOutcome.SOURCE_UNREADABLE
            if unread
            else EnumReadingOutcome.READ,
            reasons=tuple(reasons),
            consumer=handle.consumer,
            resolve_outcome=resolved.outcome,
            scan=scan,
            consumer_chain="success",
            scan_chain=scan_state,
        )
        return self._write(command, result, consumer_cells, now, backend)

    # --- the scan -------------------------------------------------------------------------------------------------

    def _scan(self, value: str, log_lines: list[str], row_cells: str) -> ModelJ4Scan:
        """Search the code, the log and the ledger row for the value; read the dummy for its marker."""
        limit = self._join.max_bytes_per_surface
        dummy = self._reading.exposure.planted_dummy
        unreadable: list[str] = []
        exposed: set[EnumJ4Surface] = set()
        dummy_found = False

        code_files = 0
        seen: set[str] = set()
        for pattern in self._join.code_globs:
            paths = self._surfaces.paths(pattern)
            if not paths:
                unreadable.append(pattern)
            for path in paths:
                if path in seen:
                    continue
                seen.add(path)
                code_files += 1
                try:
                    text = self._surfaces.read_text(path, limit)
                except OSError:
                    unreadable.append(path)
                    continue
                if value in text:
                    exposed.add(EnumJ4Surface.CODE)

        if any(value in line for line in log_lines):
            exposed.add(EnumJ4Surface.LOG)
        for pattern in self._join.log_surfaces:
            for path in self._surfaces.paths(pattern):
                try:
                    if value in self._surfaces.read_text(path, limit):
                        exposed.add(EnumJ4Surface.LOG)
                except OSError:
                    unreadable.append(path)

        if value in row_cells:
            exposed.add(EnumJ4Surface.LEDGER)

        dummy_paths = self._surfaces.paths(dummy.surface)
        if not dummy_paths:
            unreadable.append(dummy.surface)
        for path in dummy_paths:
            try:
                dummy_found = dummy_found or dummy.marker in self._surfaces.read_text(
                    path, limit
                )
            except OSError:
                unreadable.append(path)

        return ModelJ4Scan(
            code_files=code_files,
            log_lines=len(log_lines),
            ledger_rows=1,
            unreadable=tuple(unreadable),
            exposed=tuple(sorted(exposed, key=lambda s: s.value)),
            dummy_found=dummy_found,
        )

    # --- the two ways a run ends without the golden shape ---------------------------------------------------------

    def _refused(
        self,
        command: ModelJoinJ4Command,
        consumer_chain: ChainEmitter,
        code: str,
        outcome: EnumSecretProbeOutcome,
        backend: str,
        *,
        scan: ModelJ4Scan | None = None,
        now: datetime | None = None,
    ) -> ModelJoinJ4Result:
        """The consumer failed: it ends in a typed failure and the scan answers with an upstream
        refusal that never started, so the floor's fold reads the join's error shape."""
        consumer_chain.failure(code, f"{self._join.consumer} {outcome.value}")
        self._sink.emit(
            ModelNodeEmission(
                node=self._join.scan_node,
                piece=self._join.scan_piece,
                origin="drill" if self._join.drill else "probe",
                correlation_id=command.correlation_id,
                phase="failure",
                emitted_at=self._clock(),
                backend=backend,
                drill=self._join.drill,
                failure_code=UPSTREAM_PREFIX + code,
                failure_detail=f"scan not run: {code}",
            )
        )
        when = now or self._clock().astimezone(UTC)
        reasons = (code,)
        result = ModelJoinJ4Result(
            correlation_id=command.correlation_id,
            status="completed",
            verdict=EnumReadingVerdict.NOT_WORKING,
            outcome=EnumReadingOutcome.SOURCE_UNREADABLE
            if outcome.store_unread
            else EnumReadingOutcome.READ,
            reasons=reasons,
            consumer=self._join.consumer,
            resolve_outcome=outcome,
            scan=scan,
            consumer_chain="failure",
            scan_chain="upstream-refusal",
        )
        return self._write(command, result, None, when, backend)

    def _failed(self, command: ModelJoinJ4Command, why: str) -> ModelJoinJ4Result:
        return ModelJoinJ4Result(
            correlation_id=command.correlation_id, status="failed", failure=why
        )

    # --- the row --------------------------------------------------------------------------------------------------

    def _write(
        self,
        command: ModelJoinJ4Command,
        result: ModelJoinJ4Result,
        consumer_cells: str | None,
        now: datetime,
        backend: str,
    ) -> ModelJoinJ4Result:
        """Compose the join's row from its own cells, and append it on a writing run.

        The consumer's cells ride in the row only when the scan found nothing: a row is never
        written with a value in it, so a leaking consumer cannot put its key on the ledger.
        """
        assert result.verdict is not None
        assert result.outcome is not None
        scan = result.scan
        exposed = ",".join(s.value for s in scan.exposed) if scan else "none"
        key = _sha(
            "|".join((self._host, JOIN_ID, command.correlation_id, stamp_text(now)))
        )[:32]
        cells = [
            f"lane={self._join.lane}",
            _ROW_SOURCE_CELL,
            f"actor={self._join.actor}",
            "model=none",
            f"host={self._host}",
            f"join={JOIN_ID}",
            f"from_piece={self._join.consumer_piece}",
            f"to_piece={self._join.scan_piece}",
            f"tag={'drill' if self._join.drill else 'operational'}",
            f"verdict={result.verdict.value}",
            f"outcome={result.outcome.value}",
            f"reason={','.join(result.reasons) or 'none'}",
            f"consumer={result.consumer}",
            f"resolve={result.resolve_outcome.value if result.resolve_outcome else 'none'}",
            f"consumer_chain={result.consumer_chain}",
            f"scan_chain={result.scan_chain}",
            f"code_files={scan.code_files if scan else 0}",
            f"log_lines={scan.log_lines if scan else 0}",
            f"ledger_rows={scan.ledger_rows if scan else 0}",
            f"exposed={exposed or 'none'}",
            f"planted_dummy={'found' if scan and scan.dummy_found else 'missed'}",
            f"backend={backend}",
            f"probed_at={stamp_text(now)}",
            f"correlation={command.correlation_id}",
            f"key={key}",
        ]
        if consumer_cells is not None and (scan is None or not scan.exposed):
            cells.append(consumer_cells)
        cells.append(_clean(f"injected fault: {self._join.injected_fault}"))
        row = f"{stamp_text(now)} | {_ROW_TYPE} | " + " | ".join(cells)
        if not command.write:
            return result.model_copy(update={"row": row})
        assert self._appender is not None
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{key}|{row}"))
        appended = self._appender.append(row + "\n", request_id)
        if not appended.ok:
            return result.model_copy(
                update={
                    "status": "failed",
                    "ledger": f"append-failed:{appended.status}",
                    "row": row,
                    "failure": f"append-failed:{appended.status}",
                }
            )
        ledger = "written" if appended.status == "accepted" else "already-written"
        return result.model_copy(update={"ledger": ledger, "row": row})


def event_of(result: ModelJoinJ4Result) -> dict[str, object]:
    """The run as the CLI prints it: one body under the correlation id, no value."""
    return {
        "correlation_id": result.correlation_id,
        "join": JOIN_ID,
        "status": result.status,
        "verdict": result.verdict.value if result.verdict else None,
        "reasons": list(result.reasons),
        "consumer": result.consumer,
        "resolve": result.resolve_outcome.value if result.resolve_outcome else None,
        "consumer_chain": result.consumer_chain,
        "scan_chain": result.scan_chain,
        "exposed": [s.value for s in result.scan.exposed] if result.scan else [],
        "ledger": result.ledger,
        "failure": result.failure,
    }


__all__ = [
    "FAILURE_EXPOSED",
    "FAILURE_KEY_PREFIX",
    "FAILURE_SCAN_BLIND",
    "FAILURE_SCAN_UNREADABLE",
    "JOIN_ID",
    "HandlerSecretJoinJ4",
    "OverlayKeyConsumer",
    "ProtocolJ4Consumer",
    "event_of",
]
