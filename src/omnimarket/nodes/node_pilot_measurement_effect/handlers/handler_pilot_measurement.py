# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Measure every pilot the overlay declares against its declared bar; append one row.

The reading of piece 16 (pilot measurement, OMN-23619) of the base-first plan. One run reads each
pilot the overlay declares: the pilot's frozen pass bar and its measured result (through an
injected port), and always runs the pilot manifest check on the pair -- with no results when the
result is unreadable, missing or stale, so a pilot with no frozen bar or a wrong bar hash is
refused whether or not it was measured. A refused pilot reads NOT WORKING; a pilot not measured
reads UNKNOWN; a pilot that missed its frozen bar reads the overlay's rejected_reading; every
pilot measured and within its bar reads WORKING. It writes reading rows only, never a gate, a
stop or an admission. No clock is read here: the run takes its time from the injected clock, and
a port error is caught and typed (``PilotResultError``).
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

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.handlers.reading_text import stamp_text
from omnimarket.models.floor_17a import ModelNodeEmission
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_pilot_manifest_check_compute.handlers.handler_pilot_manifest_check import (
    HandlerPilotManifestCheck,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.enum_pilot_status import (
    EnumPilotStatus,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_bar_criterion import (
    ModelPilotBarCriterion,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_manifest import (
    ModelPilotManifest,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_manifest_check_request import (
    ModelPilotManifestCheckRequest,
)
from omnimarket.nodes.node_pilot_measurement_effect.models.model_pilot_measurement import (
    EnumPilotState,
    ModelPilotCounts,
    ModelPilotMeasurementCommand,
    ModelPilotMeasurementResult,
    ModelPilotOutcome,
)
from omnimarket.nodes.node_pilot_measurement_effect.overlay import (
    ModelPilotDeclaration,
    ModelPilotMeasurementOverlay,
)
from omnimarket.nodes.node_pilot_measurement_effect.protocols import (
    PilotResultError,
    ProtocolPilotResultSource,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
UPSTREAM_PREFIX = "upstream_"
_ROW_TYPE = "STATUS"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def _cell_token(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-<>" else "-" for ch in text)


class HandlerPilotMeasurement:
    """Run the pilot measurement read of the pilots the overlay declares."""

    def __init__(
        self,
        *,
        overlay: ModelPilotMeasurementOverlay,
        host: str,
        results: ProtocolPilotResultSource,
        appender: ProtocolReadingAppender | None,
        clock: Callable[[], datetime],
        emissions: ProtocolEmissionSink | None = None,
    ) -> None:
        self._emissions = emissions
        self._overlay = overlay
        self._host = host
        self._results = results
        self._appender = appender
        self._clock = clock
        self._check = HandlerPilotManifestCheck(overlay=overlay.manifest_check)

    def handle(
        self, command: ModelPilotMeasurementCommand
    ) -> ModelPilotMeasurementResult:
        chain = self._chain(command.correlation_id)
        if command.upstream_failure is not None:
            return self._refuse(command, chain is not None)
        if chain is not None:
            chain.start()
        result = self._run(command)
        if chain is not None:
            self._close_chain(chain, result)
        return result

    def _chain(self, correlation_id: str) -> ChainEmitter | None:
        emission = self._overlay.emission
        if emission is None or self._emissions is None:
            return None
        return ChainEmitter(
            self._emissions,
            node=emission.node,
            piece=emission.piece,
            clock=self._clock,
            correlation_id=correlation_id,
        )

    def _refuse(
        self, command: ModelPilotMeasurementCommand, emit: bool
    ) -> ModelPilotMeasurementResult:
        """The piece upstream failed: no start, no reading, one typed failure under the same correlation id."""
        code = command.upstream_failure or ""
        if not code.startswith(UPSTREAM_PREFIX):
            code = f"{UPSTREAM_PREFIX}{code}"
        emission = self._overlay.emission
        if emit and emission is not None and self._emissions is not None:
            self._emissions.emit(
                ModelNodeEmission(
                    node=emission.node,
                    piece=emission.piece,
                    correlation_id=command.correlation_id,
                    phase="failure",
                    emitted_at=self._clock(),
                    failure_code=code,
                    failure_detail="the upstream piece failed; no pilot was measured",
                )
            )
        return ModelPilotMeasurementResult(
            correlation_id=command.correlation_id,
            status="failed",
            host=self._host,
            failure=code,
        )

    def _close_chain(
        self, chain: ChainEmitter, result: ModelPilotMeasurementResult
    ) -> None:
        """A WORKING reading closes the chain with a success; every other end is a typed failure naming the key."""
        if result.status == "failed":
            chain.failure("run_failed", result.failure or "")
        elif result.ledger.startswith("append-failed"):
            chain.failure(
                "reading_not_written",
                ",".join(result.reasons) or result.ledger,
            )
        elif result.verdict is EnumReadingVerdict.WORKING:
            counts = result.counts
            chain.success(
                {
                    "pilots": counts.pilots if counts else 0,
                    "accepted": counts.accepted if counts else 0,
                }
            )
        elif result.verdict is EnumReadingVerdict.NOT_WORKING:
            refused = self._named(result, EnumPilotState.REFUSED)
            if refused:
                chain.failure("pilot_refused", refused)
            else:
                chain.failure(
                    "pilot_rejected", self._named(result, EnumPilotState.REJECTED)
                )
        else:
            chain.failure("pilot_unmeasured", ",".join(result.reasons))

    def _named(self, result: ModelPilotMeasurementResult, state: EnumPilotState) -> str:
        """The pilots in one state, each as its reason token."""
        return ",".join(
            token
            for outcome in result.outcomes
            if outcome.state is state
            for token in outcome.reasons
        )

    def _run(
        self, command: ModelPilotMeasurementCommand
    ) -> ModelPilotMeasurementResult:
        if command.write and self._appender is None:
            return ModelPilotMeasurementResult(
                correlation_id=command.correlation_id,
                status="failed",
                host=self._host,
                failure="no-appender-for-a-writing-run",
            )
        now = self._clock().astimezone(UTC)
        result = self._measure(command.correlation_id, now)
        return self._publish(result, now, command.write)

    def _measure(
        self, correlation_id: str, now: datetime
    ) -> ModelPilotMeasurementResult:
        outcomes = tuple(
            self._measure_pilot(declaration, correlation_id, now)
            for declaration in self._overlay.pilots
        )
        counts = ModelPilotCounts(
            pilots=len(outcomes),
            accepted=sum(o.state is EnumPilotState.ACCEPTED for o in outcomes),
            rejected=sum(o.state is EnumPilotState.REJECTED for o in outcomes),
            refused=sum(o.state is EnumPilotState.REFUSED for o in outcomes),
            unmeasured=sum(o.state is EnumPilotState.UNMEASURED for o in outcomes),
        )
        if counts.refused:
            verdict = EnumReadingVerdict.NOT_WORKING
            outcome = "refused"
        elif counts.unmeasured:
            verdict = EnumReadingVerdict.UNKNOWN
            outcome = "unmeasured"
        elif counts.rejected:
            verdict = EnumReadingVerdict(self._overlay.publish.rejected_reading)
            outcome = "rejected"
        else:
            verdict = EnumReadingVerdict.WORKING
            outcome = "measured"
        reasons = tuple(token for o in outcomes for token in o.reasons)
        if not reasons:
            reasons = (f"all-{counts.pilots}-pilots-accepted",)
        return ModelPilotMeasurementResult(
            correlation_id=correlation_id,
            status="completed",
            host=self._host,
            verdict=verdict,
            outcome=outcome,
            reasons=reasons,
            outcomes=outcomes,
            counts=counts,
        )

    def _measure_pilot(
        self,
        declaration: ModelPilotDeclaration,
        correlation_id: str,
        now: datetime,
    ) -> ModelPilotOutcome:
        """One pilot against its declared bar: the check always runs, with results only when fresh."""
        why, metrics = self._read_result(declaration, now)
        check = self._check.handle(
            ModelPilotManifestCheckRequest(
                correlation_id=uuid.uuid5(
                    uuid.NAMESPACE_URL, f"{correlation_id}|{declaration.pilot}"
                ),
                manifest=self._manifest(declaration),
                results=metrics,
            )
        )
        pilot = _cell_token(declaration.pilot)
        if check.status is EnumPilotStatus.REFUSED:
            findings = "+".join(
                _cell_token(finding.value) for finding in check.findings
            )
            return ModelPilotOutcome(
                pilot=declaration.pilot,
                state=EnumPilotState.REFUSED,
                reasons=(f"pilot-refused:{pilot}:{findings}",),
            )
        if why is not None:
            return ModelPilotOutcome(
                pilot=declaration.pilot,
                state=EnumPilotState.UNMEASURED,
                reasons=(f"pilot-unmeasured:{pilot}:{why}",),
            )
        if check.status is EnumPilotStatus.REJECTED:
            failed = "+".join(_cell_token(metric) for metric in check.failed_metrics)
            return ModelPilotOutcome(
                pilot=declaration.pilot,
                state=EnumPilotState.REJECTED,
                reasons=(f"pilot-rejected:{pilot}:{failed}",),
            )
        return ModelPilotOutcome(
            pilot=declaration.pilot, state=EnumPilotState.ACCEPTED, reasons=()
        )

    def _read_result(
        self, declaration: ModelPilotDeclaration, now: datetime
    ) -> tuple[str | None, dict[str, float] | None]:
        """The pilot's measured result, or why it is not measured: a port error is caught and typed."""
        try:
            answer = self._results.read(declaration)
        except PilotResultError:
            return "result-unreadable", None
        age = (now - answer.measured_at).total_seconds()
        if age > declaration.max_result_age_seconds:
            return "result-stale", None
        return None, dict(answer.metrics)

    def _manifest(self, declaration: ModelPilotDeclaration) -> ModelPilotManifest:
        return ModelPilotManifest(
            pilot=declaration.pilot,
            bar=tuple(
                ModelPilotBarCriterion(
                    metric=criterion.metric,
                    comparator=criterion.comparator,
                    threshold=criterion.threshold,
                )
                for criterion in declaration.bar
            ),
            bar_hash=declaration.bar_hash,
            frozen_at=declaration.frozen_at,
            started_at=declaration.started_at,
        )

    def _publish(
        self, result: ModelPilotMeasurementResult, now: datetime, write: bool
    ) -> ModelPilotMeasurementResult:
        assert result.verdict is not None
        verdict = result.verdict
        overlay_hash = self._overlay.overlay_hash()
        publish = self._overlay.publish
        key = _sha("|".join((self._host, publish.lane, stamp_text(now), overlay_hash)))[
            :32
        ]
        counts = result.counts
        cells_by_name = {
            "pilots": str(counts.pilots if counts else 0),
            "accepted": str(counts.accepted if counts else 0),
            "rejected": str(counts.rejected if counts else 0),
            "refused": str(counts.refused if counts else 0),
            "unmeasured": str(counts.unmeasured if counts else 0),
        }
        content = _sha(
            json.dumps(
                [
                    result.verdict.value if result.verdict else "",
                    result.outcome,
                    list(result.reasons),
                    cells_by_name,
                ],
                sort_keys=True,
            )
        )[:32]
        result = result.model_copy(update={"row_key": key})
        if not write:
            return result
        assert self._appender is not None
        emission = self._overlay.emission
        cells = [
            f"lane={publish.lane}",
            f"src={publish.source}",
            f"actor={publish.actor}",
            "model=none",
            f"host={self._host}",
            "drill=no",
            f"verdict={verdict.value}",
            f"outcome={result.outcome}",
            *((f"piece={emission.piece}",) if emission else ()),
            f"reason={','.join(result.reasons)}",
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
    result: ModelPilotMeasurementResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a run produces, in order, all under its correlation id."""
    if result.status == "failed":
        return [
            (
                topics["pilot_measurement_run_failed"],
                {"correlation_id": result.correlation_id, "failure": result.failure},
            )
        ]
    assert result.verdict is not None
    body: dict[str, object] = {
        "correlation_id": result.correlation_id,
        "host": result.host,
        "verdict": result.verdict.value,
        "outcome": result.outcome,
        "reasons": list(result.reasons),
        "counts": result.counts.model_dump(mode="json") if result.counts else {},
        "outcomes": [outcome.model_dump(mode="json") for outcome in result.outcomes],
        "row_key": result.row_key,
        "ledger": result.ledger,
    }
    not_written = result.ledger.startswith("append-failed")
    return [
        (
            topics["pilot_measurement_failed"]
            if not_written
            else topics["pilot_measurement_written"],
            body,
        ),
        (
            topics["pilot_measurement_run_completed"],
            {
                "correlation_id": result.correlation_id,
                "host": result.host,
                "verdict": result.verdict.value,
                "failed": int(not_written),
            },
        ),
    ]


def answer_command(
    payload: bytes, handler: HandlerPilotMeasurement, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a command is answered with a typed failure event, never dropped.
    """
    try:
        command = ModelPilotMeasurementCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [(topics["pilot_measurement_run_failed"], json.dumps(failure).encode())]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(handler.handle(command), topics)
    ]


__all__ = ["HandlerPilotMeasurement", "answer_command", "events_of", "load_topics"]
