# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host-down reading effect: one sweep probes every available lab host and publishes the readings."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.models.overlay_keys import ModelLabHostsTable
from omnimarket.nodes.node_host_down_compute.handlers.handler_host_down import (
    HandlerHostDown,
)
from omnimarket.nodes.node_host_down_compute.models.model_host_down import (
    ModelHostDownRequest,
    ModelHostObservation,
)
from omnimarket.nodes.node_host_down_reading_effect.models.model_host_down_reading import (
    EnumSweepOutcome,
    ModelHostDownRecord,
    ModelHostDownSweepRequest,
    ModelHostDownSweepResult,
)
from omnimarket.nodes.node_host_down_reading_effect.overlay import (
    ModelHostDownOverlay,
)
from omnimarket.nodes.node_host_down_reading_effect.protocols import (
    ProtocolAdvertisementSource,
    ProtocolDownStore,
    ProtocolHostProber,
    ProtocolReadingPublisher,
)


class HandlerHostDownReading:
    """One sweep: probe every available host, compute the readings, write them, read them back and publish."""

    def __init__(
        self,
        settings: ModelHostDownOverlay,
        table_source: Callable[[], ModelLabHostsTable],
        prober: ProtocolHostProber,
        advertisements: ProtocolAdvertisementSource,
        store: ProtocolDownStore,
        publisher: ProtocolReadingPublisher,
        reading_topic: str,
        sink: ProtocolEmissionSink,
        clock: Callable[[], datetime],
        backend: Literal["in_memory", "real"],
        observer: str,
    ) -> None:
        self._settings = settings
        self._table_source = table_source
        self._prober = prober
        self._advertisements = advertisements
        self._store = store
        self._publisher = publisher
        self._reading_topic = reading_topic
        self._sink = sink
        self._clock = clock
        self._backend = backend
        self._observer = observer

    def handle(self, request: ModelHostDownSweepRequest) -> ModelHostDownSweepResult:
        correlation_id = request.correlation_id or uuid4().hex
        chain = ChainEmitter(
            self._sink,
            node=self._settings.chain_node,
            piece=self._settings.piece,
            clock=lambda: self._clock().astimezone(UTC),
            correlation_id=correlation_id,
            backend=self._backend,
            origin="real",
        )
        chain.start()

        if self._observer not in self._settings.observers:
            code = self._settings.codes.observer_not_allowed
            detail = f"observer {self._observer!r} is not in the overlay's observers"
            chain.failure(code, detail)
            return ModelHostDownSweepResult(
                outcome=EnumSweepOutcome.FAILED,
                correlation_id=correlation_id,
                failure_code=code,
                detail=detail,
            )

        try:
            table = self._table_source()
            hosts = [row for row in table.hosts if row.available]
        except (OSError, ValueError) as exc:
            code = self._settings.codes.table_unreadable
            detail = f"the lab host table could not be read: {exc}"
            chain.failure(code, detail)
            return ModelHostDownSweepResult(
                outcome=EnumSweepOutcome.FAILED,
                correlation_id=correlation_id,
                failure_code=code,
                detail=detail,
            )

        observations: list[ModelHostObservation] = []
        for row in hosts:
            address = row.target.split("@", 1)[-1]
            try:
                probe_ok: bool | None = self._prober.probe(
                    row.name,
                    address,
                    self._settings.probe.port,
                    self._settings.probe.timeout_s,
                )
            except Exception:  # a probe that could not run reads UNKNOWN, never down
                probe_ok = None
            try:
                advertised_at = self._advertisements.newest(row.name)
            except OSError:
                advertised_at = None
            prior = self._store.latest(row.name)
            observations.append(
                ModelHostObservation(
                    host_name=row.name,
                    probe_ok=probe_ok,
                    first_failed_at=prior.first_failed_at if prior else None,
                    advertised_at=advertised_at,
                )
            )

        now = self._clock()
        reading = HandlerHostDown().handle(
            ModelHostDownRequest(
                evaluated_at=now,
                rules=self._settings.rules,
                observations=tuple(observations),
            )
        )

        down_hosts = reading.down_hosts
        try:
            for model_reading in reading.readings:
                record = ModelHostDownRecord(
                    host_name=model_reading.host_name,
                    state=model_reading.state,
                    first_failed_at=model_reading.first_failed_at,
                    down_since=model_reading.down_since,
                    at=now,
                )
                self._store.write(record)
        except Exception as exc:
            code = self._settings.codes.write_failed
            detail = f"the down store refused a write: {exc}"
            chain.failure(code, detail)
            return ModelHostDownSweepResult(
                outcome=EnumSweepOutcome.FAILED,
                correlation_id=correlation_id,
                failure_code=code,
                detail=detail,
            )

        stored_down = self._store.down_hosts(now, self._settings.reading_ttl_s)
        if set(stored_down) != set(down_hosts):
            code = self._settings.codes.readback_failed
            detail = (
                f"the store reads down hosts {sorted(stored_down)}, "
                f"the sweep computed {sorted(down_hosts)}"
            )
            chain.failure(code, detail)
            return ModelHostDownSweepResult(
                outcome=EnumSweepOutcome.FAILED,
                correlation_id=correlation_id,
                failure_code=code,
                detail=detail,
            )

        try:
            for model_reading in reading.readings:
                self._publisher.publish(
                    self._reading_topic,
                    model_reading.host_name,
                    model_reading.model_dump_json().encode("utf-8"),
                )
        except Exception as exc:
            code = self._settings.codes.publish_failed
            detail = f"the bus refused a reading: {exc}"
            chain.failure(code, detail)
            return ModelHostDownSweepResult(
                outcome=EnumSweepOutcome.FAILED,
                correlation_id=correlation_id,
                failure_code=code,
                detail=detail,
            )

        counts = {
            "hosts": len(reading.readings),
            "up": sum(1 for r in reading.readings if r.state.value == "up"),
            "suspect": sum(1 for r in reading.readings if r.state.value == "suspect"),
            "down": sum(1 for r in reading.readings if r.state.value == "down"),
            "unknown": sum(1 for r in reading.readings if r.state.value == "unknown"),
        }
        chain.success(counts)
        return ModelHostDownSweepResult(
            outcome=EnumSweepOutcome.SWEPT,
            correlation_id=correlation_id,
            readings=reading.readings,
            down_hosts=down_hosts,
        )
