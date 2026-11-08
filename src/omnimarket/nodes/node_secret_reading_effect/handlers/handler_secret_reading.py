# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Probe every declared consumer's key in the store, scan for exposure, write one reading row.

The piece 5 reading is WORKING only when every consumer's key resolves in its scope, no resolved
value is found on a declared ledger or log surface, the planted dummy is found (the scan's
control), every surface was read and no exposure awaits rotation. Anything else reads NOT
WORKING, with the store or a surface it could not read marked source-unreadable. It writes
reading rows only, never a gate, a stop or an admission (RULING 2026-11-06T12:05:56Z). A
resolved value is used in process for the scan and never put in a row, an event or a result.
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
from omnimarket.models.model_secret_reading import (
    ModelExposureScan,
    ModelSecretConsumerProbe,
    ModelSecretReadingCommand,
    ModelSecretReadingResult,
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
from omnimarket.nodes.node_secret_reading_effect.protocols import (
    ProtocolExposureSurfaces,
    ProtocolSecretProbeStore,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_ROW_TYPE = "STATUS"
_ROW_SOURCE_CELL = "src=secret-reading"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def judge(
    probes: tuple[ModelSecretConsumerProbe, ...],
    scan: ModelExposureScan,
    open_exposures: int,
) -> tuple[EnumReadingVerdict, EnumReadingOutcome, tuple[str, ...]]:
    """WORKING only with every consumer resolving, a clean scan that saw its planted dummy,
    every surface read and no exposure awaiting rotation (plan figure W18)."""
    reasons = [f"consumer-{p.consumer}-{p.outcome.value}" for p in probes if p.failed]
    if not probes:
        reasons.append("no-consumer-probed")
    if scan.exposures:
        reasons.append(f"value-found-on-surface-{len(scan.exposures)}")
    if not scan.dummy_found:
        reasons.append("planted-dummy-missed")
    if scan.unreadable:
        reasons.append(f"surface-unreadable-{len(scan.unreadable)}")
    if open_exposures:
        reasons.append(f"open-exposures-awaiting-rotation-{open_exposures}")
    unread = scan.unreadable or any(p.outcome.store_unread for p in probes)
    outcome = (
        EnumReadingOutcome.SOURCE_UNREADABLE if unread else EnumReadingOutcome.READ
    )
    verdict = EnumReadingVerdict.NOT_WORKING if reasons else EnumReadingVerdict.WORKING
    return verdict, outcome, tuple(reasons)


class HandlerSecretReading:
    """``handle(ModelSecretReadingCommand) -> ModelSecretReadingResult``."""

    def __init__(
        self,
        *,
        overlay: ModelSecretReadingOverlay,
        store: ProtocolSecretProbeStore,
        surfaces: ProtocolExposureSurfaces,
        appender: ProtocolReadingAppender | None,
        clock: Callable[[], datetime],
        host: str,
    ) -> None:
        self._overlay = overlay
        self._store = store
        self._surfaces = surfaces
        self._appender = appender
        self._clock = clock
        self._host = host

    def handle(self, command: ModelSecretReadingCommand) -> ModelSecretReadingResult:
        unknown = sorted(
            name for name in command.consumers if self._overlay.consumer(name) is None
        )
        if unknown:
            return self._failed(command, f"consumers-not-declared:{','.join(unknown)}")
        if command.write and self._appender is None:
            return self._failed(command, "no-appender-for-a-writing-run")
        handles = (
            tuple(
                handle
                for handle in self._overlay.consumers
                if handle.consumer in command.consumers
            )
            if command.consumers
            else self._overlay.consumers
        )
        now = self._clock().astimezone(UTC)
        probes: list[ModelSecretConsumerProbe] = []
        values: list[tuple[str, str]] = []
        for handle in handles:
            resolved = self._store.resolve(handle)
            probes.append(
                ModelSecretConsumerProbe(
                    consumer=handle.consumer,
                    folder=handle.folder,
                    key=handle.key,
                    outcome=resolved.outcome,
                    verdict=resolved.outcome.verdict,
                )
            )
            if resolved.value is not None:
                values.append((handle.consumer, resolved.value.get_secret_value()))
        scan = self._scan(values)
        del values
        open_exposures = len(self._overlay.open_exposures)
        verdict, outcome, reasons = judge(tuple(probes), scan, open_exposures)
        key = _sha(
            "|".join(
                (self._host, "secrets", stamp_text(now), self._overlay.overlay_hash())
            )
        )[:32]
        result = ModelSecretReadingResult(
            correlation_id=command.correlation_id,
            status="completed",
            verdict=verdict,
            outcome=outcome,
            reasons=reasons,
            probes=tuple(probes),
            scan=scan,
            open_exposures=open_exposures,
            row_key=key,
        )
        if not command.write:
            return result
        return self._write(result, now)

    def _scan(self, values: list[tuple[str, str]]) -> ModelExposureScan:
        """Search every surface for every resolved value and the dummy for its marker."""
        settings = self._overlay.exposure
        dummy = settings.planted_dummy
        patterns = (*settings.surfaces, dummy.surface)
        unreadable: list[str] = []
        exposures: list[str] = []
        dummy_found = False
        seen: set[str] = set()
        for pattern in patterns:
            paths = self._surfaces.paths(pattern)
            if not paths:
                unreadable.append(pattern)
            for path in paths:
                if path in seen:
                    continue
                seen.add(path)
                try:
                    text = self._surfaces.read_text(
                        path, settings.max_bytes_per_surface
                    )
                except OSError:
                    unreadable.append(path)
                    continue
                if pattern == dummy.surface and dummy.marker in text:
                    dummy_found = True
                exposures.extend(
                    consumer for consumer, value in values if value in text
                )
        return ModelExposureScan(
            surfaces=len(seen),
            unreadable=tuple(unreadable),
            exposures=tuple(sorted(exposures)),
            dummy_found=dummy_found,
        )

    def _failed(
        self, command: ModelSecretReadingCommand, why: str
    ) -> ModelSecretReadingResult:
        return ModelSecretReadingResult(
            correlation_id=command.correlation_id, status="failed", failure=why
        )

    def _write(
        self, result: ModelSecretReadingResult, now: datetime
    ) -> ModelSecretReadingResult:
        assert self._appender is not None
        assert result.verdict is not None
        assert result.outcome is not None
        assert result.scan is not None
        publish = self._overlay.publish
        failed = [probe for probe in result.probes if probe.failed]
        content = _sha(
            json.dumps(
                [
                    result.verdict.value,
                    [(p.consumer, p.outcome.value) for p in result.probes],
                    list(result.reasons),
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
            f"piece={publish.piece}",
            f"tag={'drill' if publish.drill else 'operational'}",
            f"drill={'yes' if publish.drill else 'no'}",
            f"verdict={result.verdict.value}",
            f"outcome={result.outcome.value}",
            f"reason={','.join(result.reasons) or 'none'}",
            f"consumers={len(result.probes)}",
            f"passed={len(result.probes) - len(failed)}",
            "failed_consumers="
            + (",".join(f"{p.consumer}:{p.outcome.value}" for p in failed) or "none"),
            f"surfaces={result.scan.surfaces}",
            f"exposures={len(result.scan.exposures)}",
            f"planted_dummy={'found' if result.scan.dummy_found else 'missed'}",
            f"open_exposures={result.open_exposures}",
            f"backend={self._overlay.store.backend_name}",
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
    result: ModelSecretReadingResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a run produces, in order, all under its correlation id."""
    events: list[tuple[str, dict[str, object]]] = [
        (
            topics["secret_probe_failed"]
            if probe.failed
            else topics["secret_probe_passed"],
            {
                "correlation_id": result.correlation_id,
                "consumer": probe.consumer,
                "folder": probe.folder,
                "key": probe.key,
                "outcome": probe.outcome.value,
                "verdict": probe.verdict.value,
            },
        )
        for probe in result.probes
    ]
    if result.status == "failed":
        events.append(
            (
                topics["secret_reading_run_failed"],
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
            topics["secret_reading_run_completed"],
            {
                "correlation_id": result.correlation_id,
                "verdict": result.verdict.value,
                "reasons": list(result.reasons),
                "consumers": len(result.probes),
                "failed": sum(probe.failed for probe in result.probes),
                "row_key": result.row_key,
                "ledger": result.ledger,
            },
        )
    )
    return events


def answer_command(
    payload: bytes, handler: HandlerSecretReading, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a command is answered with a typed failure event, never dropped.
    """
    try:
        command = ModelSecretReadingCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [(topics["secret_reading_run_failed"], json.dumps(failure).encode())]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(handler.handle(command), topics)
    ]


__all__ = [
    "HandlerSecretReading",
    "answer_command",
    "events_of",
    "judge",
    "load_topics",
]
