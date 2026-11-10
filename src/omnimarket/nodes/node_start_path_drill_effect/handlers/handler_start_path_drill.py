# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The start-path census and refusal drill: one run, one reading (OMN-23306, step 0 row 4, rebuild plan W28).

For every path of the census the drill asks the start-admission effect, on a test gate store, for two starts: a
planted one on a piece that is under a stop, which must be refused, and a control one on a piece the policy
admits, which must be admitted. The effect runs in ``enforce`` mode for the drill whatever a path's real mode
is, and every request is tagged ``drill``. The reading is WORKING only when

* the census holds every start path the contract declares and every path the admission policy names,
* every planted start is refused (a planted start that proceeds is a bypass) and every control start is admitted,
* every decision reached the observe log.

A dead path is counted and not drilled; an unbound path is drilled by its id and counted as unbound, since the
drill proves the decision a path would get and the binding column says whether the path asks yet. The reading is
information: it decides no start (RULING 2026-11-06T12:05:56Z).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.handlers.reading_text import reason_token, stamp_text
from omnimarket.models.model_gate_store import (
    EnumGateStoreBackend,
    EnumGateStoreOperation,
    ModelGateStorePolicy,
    ModelGateStoreRequest,
)
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.models.model_start_admission import (
    EnumAdmissionMode,
    EnumAdmissionOutcome,
    ModelStartAdmissionLogRow,
    ModelStartAdmissionPolicy,
    ModelStartAdmissionRequest,
)
from omnimarket.nodes.node_gate_store_effect.handlers.store_in_memory import (
    InMemoryGateStore,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)
from omnimarket.nodes.node_start_admission_effect.handlers.handler_start_admission import (
    HandlerStartAdmission,
)
from omnimarket.nodes.node_start_path_drill_effect.models.model_start_path_drill import (
    ModelPathDrilled,
    ModelStartPathDrillCommand,
    ModelStartPathDrillResult,
)
from omnimarket.nodes.node_start_path_drill_effect.overlay import (
    EnumPathBinding,
    ModelDrillGate,
    ModelStartPathDrillOverlay,
)
from omnimarket.nodes.node_start_path_drill_effect.protocols import (
    ProtocolDrillStoreFactory,
)
from omnimarket.protocols.protocol_start_admission import ProtocolStartAdmissionLog

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_ROW_TYPE = "STATUS"
_ROW_SOURCE_CELL = "src=start-path-drill"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def load_declared_paths(path: Path = CONTRACT) -> tuple[str, ...]:
    """The start paths the contract declares: the plan's nine and the second reader's six."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return tuple(contract["start_paths"])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def in_memory_drill_store(gate: ModelDrillGate) -> ProtocolDrillStoreFactory:
    """A factory of test gate stores: the stopped piece is under a stop, the open piece has a limit."""

    def build() -> InMemoryGateStore:
        store = InMemoryGateStore(
            ModelGateStorePolicy(
                backend=EnumGateStoreBackend.IN_MEMORY,
                limits={gate.stopped_piece: 1, gate.open_piece: gate.open_piece_limit},
                reservation_ttl_seconds=gate.reservation_ttl_seconds,
                order=(),
            )
        )
        store.transact(
            ModelGateStoreRequest(
                correlation_id="drill-setup",
                operation=EnumGateStoreOperation.RECORD_SAFETY,
                step_key="drill-setup/stop",
                piece=gate.stopped_piece,
                safety_state="stop",
            )
        )
        return store

    return build


class _CountingLog:
    """Counts the rows the drill's decisions send to the observe log, and the ones it did not take."""

    def __init__(self, inner: ProtocolStartAdmissionLog) -> None:
        self._inner = inner
        self.attempted = 0
        self.raised = 0

    def append(self, row: ModelStartAdmissionLogRow) -> None:
        self.attempted += 1
        try:
            self._inner.append(row)
        except Exception:  # a log fault is counted and never undoes the decision
            self.raised += 1

    @property
    def failed(self) -> int:
        refused = getattr(self._inner, "failed", [])
        return self.raised + len(refused)


class HandlerStartPathDrill:
    """Run the census and refusal drill once."""

    def __init__(
        self,
        *,
        overlay: ModelStartPathDrillOverlay,
        host: str,
        declared_paths: Sequence[str],
        admission_paths: Sequence[str],
        log: ProtocolStartAdmissionLog,
        appender: ProtocolReadingAppender | None,
        clock: Callable[[], datetime],
        store_factory: ProtocolDrillStoreFactory | None = None,
    ) -> None:
        self._overlay = overlay
        self._host = host
        self._declared = tuple(declared_paths)
        self._admission = tuple(admission_paths)
        self._log = log
        self._appender = appender
        self._clock = clock
        self._store_factory = store_factory or in_memory_drill_store(overlay.gate)

    def handle(self, command: ModelStartPathDrillCommand) -> ModelStartPathDrillResult:
        if command.write and self._appender is None:
            return ModelStartPathDrillResult(
                correlation_id=command.correlation_id,
                status="failed",
                host=self._host,
                failure="no-appender-for-a-writing-run",
            )
        now = self._clock().astimezone(UTC)
        return self._publish(self._drill(command.correlation_id, now), now, command)

    def _drill(self, correlation_id: str, now: datetime) -> ModelStartPathDrillResult:
        census = self._overlay.census
        gate = self._overlay.gate
        in_census = {path.id for path in census}
        missing = tuple(
            sorted((set(self._declared) | set(self._admission)) - in_census)
        )
        policy = ModelStartAdmissionPolicy(
            default_mode=EnumAdmissionMode.ENFORCE,
            modes={},
            answer_deadline_seconds=gate.answer_deadline_seconds,
            max_redecisions=gate.max_redecisions,
        )
        log = _CountingLog(self._log)
        drilled: list[ModelPathDrilled] = []
        stamp = now.timestamp()
        for entry in census:
            if entry.binding is EnumPathBinding.DEAD:
                continue
            admission = HandlerStartAdmission(self._store_factory(), log, policy)
            planted = admission.handle(
                self._request(
                    correlation_id, entry.id, "planted", gate.stopped_piece, stamp
                )
            )
            control = admission.handle(
                self._request(
                    correlation_id, entry.id, "control", gate.open_piece, stamp
                )
            )
            drilled.append(
                ModelPathDrilled(
                    path=entry.id,
                    binding=entry.binding.value,
                    planted_refused=not planted.proceed,
                    control_admitted=control.proceed
                    and control.outcome is EnumAdmissionOutcome.ADMITTED,
                    planted_outcome=planted.outcome.value,
                    control_outcome=control.outcome.value,
                )
            )
        flush = getattr(self._log, "flush", None)
        if callable(flush):
            try:
                flush()
            except (
                Exception
            ):  # a log fault is counted below and never undoes the decision
                log.raised += 1
        bypassed = [d.path for d in drilled if not d.planted_refused]
        control_refused = [d.path for d in drilled if not d.control_admitted]
        reasons: list[str] = []
        if not census:
            reasons.append("census-empty")
        if missing:
            reasons.append(f"census-missing-{','.join(missing)}")
        if bypassed:
            reasons.append(f"planted-start-admitted-{','.join(bypassed)}")
        if control_refused:
            reasons.append(f"control-start-not-admitted-{','.join(control_refused)}")
        if log.failed:
            reasons.append(f"observe-log-append-failed-{log.failed}")
        if log.attempted != 2 * len(drilled):
            reasons.append(f"observe-log-short-{log.attempted}-of-{2 * len(drilled)}")
        counts = {
            binding: sum(1 for p in census if p.binding is binding)
            for binding in EnumPathBinding
        }
        return ModelStartPathDrillResult(
            correlation_id=correlation_id,
            status="completed",
            host=self._host,
            verdict=EnumReadingVerdict.NOT_WORKING
            if reasons
            else EnumReadingVerdict.WORKING,
            outcome=EnumReadingOutcome.READ,
            reasons=tuple(reasons) or ("every-planted-start-refused",),
            census_paths=len(census),
            census_missing=missing,
            bound=counts[EnumPathBinding.BOUND],
            unbound=counts[EnumPathBinding.UNBOUND],
            dead=counts[EnumPathBinding.DEAD],
            refused=sum(1 for d in drilled if d.planted_refused),
            admitted=len(bypassed),
            control_admitted=sum(1 for d in drilled if d.control_admitted),
            paths=tuple(drilled),
            log_written=log.attempted - log.failed,
            log_failed=log.failed,
        )

    @staticmethod
    def _request(
        correlation_id: str, path: str, kind: str, piece: str, now: float
    ) -> ModelStartAdmissionRequest:
        return ModelStartAdmissionRequest(
            correlation_id=correlation_id,
            step_key=f"{correlation_id}/{path}/{kind}",
            path=path,
            piece=piece,
            reservation_id=f"{correlation_id}/{path}/{kind}",
            drill=True,
            now=now,
        )

    def _publish(
        self,
        result: ModelStartPathDrillResult,
        now: datetime,
        command: ModelStartPathDrillCommand,
    ) -> ModelStartPathDrillResult:
        overlay_hash = self._overlay.overlay_hash()
        key = _sha("|".join((self._host, stamp_text(now), overlay_hash)))[:32]
        result = result.model_copy(update={"row_key": key})
        if not command.write:
            return result
        assert self._appender is not None
        publish = self._overlay.publish
        cells_by_name = {
            "census": str(result.census_paths),
            "bound": str(result.bound),
            "unbound": str(result.unbound),
            "dead": str(result.dead),
            "refused": str(result.refused),
            "admitted": str(result.admitted),
            "control_admitted": str(result.control_admitted),
            "census_missing": ",".join(result.census_missing) or "none",
            "log_written": str(result.log_written),
            "log_failed": str(result.log_failed),
        }
        content = _sha(
            json.dumps(
                [
                    result.verdict.value,
                    result.outcome.value,
                    list(result.reasons),
                    cells_by_name,
                ],
                sort_keys=True,
            )
        )[:32]
        cells = [
            f"lane={publish.lane}",
            _ROW_SOURCE_CELL,
            f"actor={publish.actor}",
            "model=none",
            f"host={self._host}",
            "drill=1",
            f"verdict={result.verdict.value}",
            f"outcome={result.outcome.value}",
            f"reason={reason_token(','.join(result.reasons))}",
            *(f"{name}={value}" for name, value in cells_by_name.items()),
            f"probed_at={stamp_text(now)}",
            f"published_at={stamp_text(now)}",
            f"max_age={publish.publish_max_age_seconds}",
            f"key={key}",
            f"content={content}",
            f"overlay_hash={overlay_hash[:12]}",
            _clean(f"injected fault: {self._overlay.injected_fault}"),
        ]
        row = f"{stamp_text(now)} | {_ROW_TYPE} | " + " | ".join(cells)
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{key}|{content}"))
        appended = self._appender.append(row + "\n", request_id)
        if not appended.ok:
            return result.model_copy(
                update={"ledger": f"append-failed:{appended.status}", "row": row}
            )
        return result.model_copy(
            update={
                "ledger": "written"
                if appended.status == "accepted"
                else "already-written",
                "row": row,
            }
        )


def events_of(
    result: ModelStartPathDrillResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a run produces, in order, all under its correlation id."""
    if result.status == "failed":
        return [
            (
                topics["start_path_drill_run_failed"],
                {"correlation_id": result.correlation_id, "failure": result.failure},
            )
        ]
    body: dict[str, object] = {
        "correlation_id": result.correlation_id,
        "host": result.host,
        "verdict": result.verdict.value,
        "outcome": result.outcome.value,
        "reasons": list(result.reasons),
        "refused": result.refused,
        "admitted": result.admitted,
        "census_paths": result.census_paths,
        "row_key": result.row_key,
        "ledger": result.ledger,
    }
    return [
        (
            topics["start_path_drill_failed"]
            if result.failed
            else topics["start_path_drill_written"],
            body,
        ),
        (
            topics["start_path_drill_run_completed"],
            {
                "correlation_id": result.correlation_id,
                "host": result.host,
                "verdict": result.verdict.value,
                "failed": int(result.failed),
            },
        ),
    ]


def answer_command(
    payload: bytes, handler: HandlerStartPathDrill, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a command is answered with a typed failure event, never dropped.
    """
    try:
        command = ModelStartPathDrillCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [(topics["start_path_drill_run_failed"], json.dumps(failure).encode())]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(handler.handle(command), topics)
    ]


__all__ = [
    "HandlerStartPathDrill",
    "answer_command",
    "events_of",
    "in_memory_drill_store",
    "load_declared_paths",
    "load_topics",
]
