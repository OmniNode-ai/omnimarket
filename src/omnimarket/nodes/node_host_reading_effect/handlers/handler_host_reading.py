# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read one host's declared probes now and append one row per probe.

It writes host reading rows only, never a gate, a stop or an admission. A probe that cannot read
its source gives an UNKNOWN row. A verdict of NOT_WORKING is a reading, not a failure of the run;
a read that failed or an append that did not land is.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.handlers.emission_sink import ProtocolEmissionSink
from omnimarket.models.model_host_reading import (
    EnumHostProbe,
    ModelHostProbeOutcome,
    ModelHostReadingCommand,
    ModelHostReadingResult,
    ModelProbeReading,
)
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_host_reading_effect.handlers.probe_host_canary import (
    read_canary,
)
from omnimarket.nodes.node_host_reading_effect.handlers.probe_join_j1 import (
    read_join_j1,
)
from omnimarket.nodes.node_host_reading_effect.handlers.probe_ledger_durability import (
    read_durability,
)
from omnimarket.nodes.node_host_reading_effect.handlers.probe_ledger_old_names import (
    read_old_names,
)
from omnimarket.nodes.node_host_reading_effect.handlers.probe_ledger_outbox import (
    read_outbox,
)
from omnimarket.nodes.node_host_reading_effect.overlay import ModelHostReadingOverlay
from omnimarket.nodes.node_host_reading_effect.protocols import (
    ProtocolAdvertisementSource,
    ProtocolCanaryRunner,
    ProtocolGraphVerdictSource,
    ProtocolHostFiles,
    ProtocolHostInventory,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    reason_token,
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolLedgerRowSource,
    ProtocolReadingAppender,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_ROW_TYPE = "STATUS"
_ROW_SOURCE_CELL = "src=host-reading"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


class HandlerHostReading:
    """Run the probes the overlay declares for this host."""

    def __init__(
        self,
        *,
        overlay: ModelHostReadingOverlay,
        host: str,
        files: ProtocolHostFiles,
        appender: ProtocolReadingAppender | None,
        clock: Callable[[], datetime],
        environ: Mapping[str, str],
        probe_appender: ProtocolReadingAppender | None = None,
        durable_reader: ProtocolLedgerRowSource | None = None,
        sleep: Callable[[float], None] = time.sleep,
        inventory: ProtocolHostInventory | None = None,
        canary_runner: ProtocolCanaryRunner | None = None,
        nonce_for: Callable[[str], str] | None = None,
        adverts: ProtocolAdvertisementSource | None = None,
        graph_source: ProtocolGraphVerdictSource | None = None,
        sink: ProtocolEmissionSink | None = None,
    ) -> None:
        self._overlay = overlay
        self._host = host
        self._files = files
        self._appender = appender
        self._clock = clock
        self._environ = environ
        # The durability probe's own routes: its probe row and its read of the durable store.
        self._probe_appender = probe_appender
        self._durable_reader = durable_reader
        self._sleep = sleep
        self._inventory = inventory
        self._canary_runner = canary_runner
        self._adverts = adverts
        self._graph_source = graph_source
        self._sink = sink
        self._nonce_for = nonce_for or (lambda _host: uuid.uuid4().hex[:16])

    def handle(self, command: ModelHostReadingCommand) -> ModelHostReadingResult:
        host = self._overlay.host(self._host)
        if host is None:
            return self._failed(command, f"host-not-declared:{self._host}")
        unknown = sorted(set(command.probes) - set(host.probes))
        if unknown:
            names = ",".join(probe.value for probe in unknown)
            return self._failed(command, f"probes-not-declared:{names}")
        if command.write and self._appender is None:
            return self._failed(command, "no-appender-for-a-writing-run")
        wanted = command.probes or host.probes
        now = self._clock().astimezone(UTC)
        return ModelHostReadingResult(
            correlation_id=command.correlation_id,
            status="completed",
            host=self._host,
            probes=tuple(self._probe(probe, now, command.write) for probe in wanted),
        )

    def _failed(
        self, command: ModelHostReadingCommand, why: str
    ) -> ModelHostReadingResult:
        return ModelHostReadingResult(
            correlation_id=command.correlation_id,
            status="failed",
            host=self._host,
            failure=why,
        )

    def _read(
        self, probe: EnumHostProbe, now: datetime, write: bool
    ) -> ModelProbeReading:
        try:
            if probe is EnumHostProbe.LEDGER_OUTBOX:
                return read_outbox(
                    self._overlay.outbox, self._files, self._environ, now
                )
            if probe is EnumHostProbe.LEDGER_DURABILITY:
                return self._read_durability()
            if probe is EnumHostProbe.HOST_CANARY:
                if (
                    self._overlay.canary is None
                    or self._inventory is None
                    or self._canary_runner is None
                ):
                    return ModelProbeReading(
                        probe=probe,
                        verdict=EnumReadingVerdict.UNKNOWN,
                        outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
                        reasons=("canary-ports-not-wired",),
                    )
                return read_canary(
                    self._overlay.canary,
                    self._inventory,
                    self._canary_runner,
                    self._environ,
                    self._nonce_for,
                    self._adverts,
                    now,
                )
            if probe is EnumHostProbe.JOIN_J1:
                if self._overlay.join_j1 is None or self._graph_source is None:
                    return ModelProbeReading(
                        probe=probe,
                        verdict=EnumReadingVerdict.UNKNOWN,
                        outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
                        reasons=("join-j1-ports-not-wired",),
                    )
                return read_join_j1(
                    self._overlay.join_j1,
                    self._graph_source,
                    self._sink if write else None,
                    correlation_id=f"graph-run-{self._nonce_for(self._host)}",
                    clock=self._clock,
                )[1]
            assert self._overlay.old_names is not None
            return read_old_names(self._overlay.old_names, self._files, self._environ)
        except OSError as exc:
            return ModelProbeReading(
                probe=probe,
                verdict=EnumReadingVerdict.UNKNOWN,
                outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
                reasons=(f"host-read-failed-{reason_token(type(exc).__name__)}",),
            )

    def _read_durability(self) -> ModelProbeReading:
        settings = self._overlay.durability
        assert settings is not None
        if self._probe_appender is None or self._durable_reader is None:
            return ModelProbeReading(
                probe=EnumHostProbe.LEDGER_DURABILITY,
                verdict=EnumReadingVerdict.UNKNOWN,
                outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
                reasons=("durability-routes-not-wired",),
            )
        return read_durability(
            settings,
            host=self._host,
            actor=self._overlay.publish.actor,
            appender=self._probe_appender,
            reader=self._durable_reader,
            clock=self._clock,
            sleep=self._sleep,
        )

    def _fault(self, probe: EnumHostProbe) -> str:
        if probe is EnumHostProbe.LEDGER_OUTBOX:
            return self._overlay.outbox.injected_fault
        if probe is EnumHostProbe.LEDGER_DURABILITY:
            assert self._overlay.durability is not None
            return self._overlay.durability.injected_fault
        if probe is EnumHostProbe.HOST_CANARY:
            assert self._overlay.canary is not None
            return self._overlay.canary.injected_fault
        if probe is EnumHostProbe.JOIN_J1:
            assert self._overlay.join_j1 is not None
            return self._overlay.join_j1.injected_fault
        assert self._overlay.old_names is not None
        return self._overlay.old_names.injected_fault

    def _probe(
        self, probe: EnumHostProbe, now: datetime, write: bool
    ) -> ModelHostProbeOutcome:
        reading = self._read(probe, now, write)
        overlay_hash = self._overlay.overlay_hash()
        key = _sha("|".join((self._host, probe.value, stamp_text(now), overlay_hash)))[
            :32
        ]
        content = _sha(
            json.dumps(
                [
                    reading.verdict.value,
                    reading.outcome.value,
                    list(reading.reasons),
                    reading.cells,
                ],
                sort_keys=True,
            )
        )[:32]

        def outcome(ledger: str, row: str | None = None) -> ModelHostProbeOutcome:
            return ModelHostProbeOutcome(
                probe=probe,
                host=self._host,
                verdict=reading.verdict,
                outcome=reading.outcome,
                reasons=reading.reasons,
                cells=reading.cells,
                row_key=key,
                ledger=ledger,
                row=row,
            )

        if not write:
            return outcome("not-written")
        assert self._appender is not None
        publish = self._overlay.publish
        cells = [
            f"lane={publish.lane}",
            _ROW_SOURCE_CELL,
            f"actor={publish.actor}",
            "model=none",
            f"host={self._host}",
            f"probe={probe.value}",
            f"verdict={reading.verdict.value}",
            f"outcome={reading.outcome.value}",
            f"reason={','.join(reading.reasons)}",
            *(f"{name}={value}" for name, value in sorted(reading.cells.items())),
            f"probed_at={stamp_text(now)}",
            f"published_at={stamp_text(now)}",
            f"max_age={publish.publish_max_age_seconds}",
            f"key={key}",
            f"content={content}",
            f"overlay_hash={overlay_hash[:12]}",
            _clean(f"injected fault: {self._fault(probe)}"),
        ]
        row = f"{stamp_text(now)} | {_ROW_TYPE} | " + " | ".join(cells)
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{key}|{content}"))
        appended = self._appender.append(row + "\n", request_id)
        if not appended.ok:
            return outcome(f"append-failed:{appended.status}", row)
        return outcome(
            "written" if appended.status == "accepted" else "already-written", row
        )


def events_of(
    result: ModelHostReadingResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a run produces, in order, all under its correlation id."""
    if result.status == "failed":
        return [
            (
                topics["host_reading_run_failed"],
                {"correlation_id": result.correlation_id, "failure": result.failure},
            )
        ]
    events: list[tuple[str, dict[str, object]]] = [
        (
            topics["host_reading_failed"]
            if probe.failed
            else topics["host_reading_written"],
            {
                "correlation_id": result.correlation_id,
                "host": probe.host,
                "probe": probe.probe.value,
                "verdict": probe.verdict.value,
                "outcome": probe.outcome.value,
                "reasons": list(probe.reasons),
                "cells": dict(probe.cells),
                "row_key": probe.row_key,
                "ledger": probe.ledger,
            },
        )
        for probe in result.probes
    ]
    events.append(
        (
            topics["host_reading_run_completed"],
            {
                "correlation_id": result.correlation_id,
                "host": result.host,
                "probes": len(result.probes),
                "failed": sum(probe.failed for probe in result.probes),
            },
        )
    )
    return events


def answer_command(
    payload: bytes, handler: HandlerHostReading, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a command is answered with a typed failure event, never dropped.
    """
    try:
        command = ModelHostReadingCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [(topics["host_reading_run_failed"], json.dumps(failure).encode())]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(handler.handle(command), topics)
    ]
