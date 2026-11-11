# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Gather the facts, read piece 11 through the compute node, write one reading row (OMN-22958).

The judgement is HandlerDevDeployReading's; this handler supplies the facts and the overlay's thresholds,
and appends the row. It writes reading rows only, never a gate, a stop or an admission (RULING
2026-11-06T12:05:56Z). A drill run (the drill verb) adds the overlay's injected fault, a runtime-affecting merge nobody
receipted, so the error event stays fresh without breaking the running lane.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.models.model_dev_deploy_reading import (
    ModelDevDeployReadingRequest,
    ModelRuntimeMerge,
)
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_dev_deploy_reading_compute.handlers.handler_dev_deploy_reading import (
    HandlerDevDeployReading,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.models.model_dev_deploy_reading_run import (
    ModelDevDeployFacts,
    ModelDevDeployReadingCommand,
    ModelDevDeployReadingResult,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.overlay import (
    ModelDevDeployReadingOverlay,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.protocols import (
    ProtocolDevDeployFactSource,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_ROW_TYPE = "STATUS"
_ROW_SOURCE_CELL = "src=dev-deploy-reading"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def _is_receipt_reason(reason: str) -> bool:
    return not reason.startswith(("probe-", "probes-unreadable"))


class HandlerDevDeployReadingRun:
    """``handle(ModelDevDeployReadingCommand) -> ModelDevDeployReadingResult``."""

    def __init__(
        self,
        *,
        overlay: ModelDevDeployReadingOverlay,
        source: ProtocolDevDeployFactSource,
        appender: ProtocolReadingAppender | None,
        clock: Callable[[], datetime],
        host: str,
        drill: bool = False,
        compute: HandlerDevDeployReading | None = None,
    ) -> None:
        self._drill = drill
        self._overlay = overlay
        self._source = source
        self._appender = appender
        self._clock = clock
        self._host = host
        self._compute = compute or HandlerDevDeployReading()

    def handle(
        self, command: ModelDevDeployReadingCommand
    ) -> ModelDevDeployReadingResult:
        if command.write and self._appender is None:
            return ModelDevDeployReadingResult(
                correlation_id=command.correlation_id,
                status="failed",
                failure="no-appender-for-a-writing-run",
            )
        now = self._clock().astimezone(UTC)
        facts = self._source.read(now)
        fault = self._overlay.drill_fault
        merges = facts.merges
        if self._drill:
            merges = (
                *merges,
                ModelRuntimeMerge(
                    sha=fault.synthetic_merge_sha,
                    merged_at=now - timedelta(seconds=fault.age_seconds),
                    runtime_affecting=True,
                ),
            )
        thresholds = self._overlay.thresholds
        try:
            reading = self._compute.handle(
                ModelDevDeployReadingRequest(
                    now=now,
                    required_lane=thresholds.required_lane,
                    receipt_deadline_seconds=thresholds.receipt_deadline_seconds,
                    probe_max_age_seconds=thresholds.probe_max_age_seconds,
                    merges=merges,
                    receipts=facts.receipts,
                    probes=facts.probes,
                    lineage_rule=self._overlay.lineage,
                    lineage=facts.lineage,
                    receipts_unread_reason=facts.receipts_unread_reason,
                    probes_unread_reason=facts.probes_unread_reason,
                )
            )
        except ValidationError as exc:
            return ModelDevDeployReadingResult(
                correlation_id=command.correlation_id,
                status="failed",
                failure=f"facts-refused:{sorted({'.'.join(map(str, e['loc'])) for e in exc.errors()})}",
            )
        unread = bool(facts.receipts_unread_reason or facts.probes_unread_reason)
        outcome = (
            EnumReadingOutcome.SOURCE_UNREADABLE
            if unread and reading.verdict is EnumReadingVerdict.UNKNOWN
            else EnumReadingOutcome.READ
        )
        key = _sha(
            "|".join(
                (
                    self._host,
                    "dev-deploy",
                    stamp_text(now),
                    self._overlay.overlay_hash(),
                )
            )
        )[:32]
        result = ModelDevDeployReadingResult(
            correlation_id=command.correlation_id,
            status="completed",
            verdict=reading.verdict,
            outcome=outcome,
            reasons=reading.reasons,
            pending=reading.pending,
            merges_checked=reading.merges_checked,
            merges_receipted=reading.merges_receipted,
            probe_age_seconds=reading.probe_age_seconds,
            row_key=key,
        )
        if not command.write:
            return result
        return self._write(result, now)

    def _write(
        self, result: ModelDevDeployReadingResult, now: datetime
    ) -> ModelDevDeployReadingResult:
        assert self._appender is not None
        assert result.verdict is not None
        assert result.outcome is not None
        publish = self._overlay.publish
        content = _sha(
            json.dumps(
                [result.verdict.value, list(result.reasons), list(result.pending)],
                sort_keys=True,
            )
        )[:32]
        cells = [
            f"lane={publish.lane}",
            _ROW_SOURCE_CELL,
            f"actor={publish.actor}",
            "model=none",
            f"host={self._host}",
            f"piece={publish.piece}",
            f"tag={'drill' if self._drill else 'operational'}",
            f"drill={'yes' if self._drill else 'no'}",
            f"verdict={result.verdict.value}",
            f"outcome={result.outcome.value}",
            f"reason={','.join(result.reasons) or 'none'}",
            f"pending={','.join(result.pending) or 'none'}",
            f"merges_checked={result.merges_checked}",
            f"merges_receipted={result.merges_receipted}",
            "probe_age_s="
            + (
                "none"
                if result.probe_age_seconds is None
                else str(result.probe_age_seconds)
            ),
            f"probed_at={stamp_text(now)}",
            f"published_at={stamp_text(now)}",
            f"max_age={publish.publish_max_age_seconds}",
            f"key={result.row_key}",
            f"content={content}",
            f"overlay_hash={self._overlay.overlay_hash()[:12]}",
            _clean(f"injected fault: {self._overlay.injected_fault}"),
        ]
        row = f"{stamp_text(now)} | {_ROW_TYPE} | " + " | ".join(cells)
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{result.row_key}|{content}"))
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


def events_of(
    result: ModelDevDeployReadingResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a run produces, in order, all under its correlation id.

    One check event per rule (receipts, probe), passed or failed by the reasons that belong to it, then the run event.
    """
    events: list[tuple[str, dict[str, object]]] = []
    if result.status == "completed":
        for rule, mine in (
            ("receipts", [r for r in result.reasons if _is_receipt_reason(r)]),
            ("probe", [r for r in result.reasons if not _is_receipt_reason(r)]),
        ):
            events.append(
                (
                    topics[
                        "dev_deploy_check_failed" if mine else "dev_deploy_check_passed"
                    ],
                    {
                        "correlation_id": result.correlation_id,
                        "rule": rule,
                        "reasons": mine,
                    },
                )
            )
    if result.status == "failed":
        events.append(
            (
                topics["dev_deploy_reading_run_failed"],
                {
                    "correlation_id": result.correlation_id,
                    "failure": result.failure,
                    "ledger": result.ledger,
                },
            )
        )
        return events
    assert result.verdict is not None
    events.append(
        (
            topics["dev_deploy_reading_run_completed"],
            {
                "correlation_id": result.correlation_id,
                "verdict": result.verdict.value,
                "reasons": list(result.reasons),
                "merges_checked": result.merges_checked,
                "merges_receipted": result.merges_receipted,
                "row_key": result.row_key,
                "ledger": result.ledger,
            },
        )
    )
    return events


def answer_command(
    payload: bytes, handler: HandlerDevDeployReadingRun, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a command is answered with a typed failure event, never dropped.
    """
    try:
        command = ModelDevDeployReadingCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [(topics["dev_deploy_reading_run_failed"], json.dumps(failure).encode())]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(handler.handle(command), topics)
    ]


__all__ = [
    "HandlerDevDeployReadingRun",
    "ModelDevDeployFacts",
    "answer_command",
    "events_of",
    "load_topics",
]
