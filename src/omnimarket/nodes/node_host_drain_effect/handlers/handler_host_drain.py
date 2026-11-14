# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The drain verb (rebuild plan 7.4, OMN-22905): a host leaves placement by a command, not by a data pull request.

``drain`` sets the host draining in the drain store and reads it back; the mark is time-bounded, so the host reads
not draining again once its ttl is past (OMN-24489); ``check_start`` refuses a new lane for a draining host; running
lanes are counted and left to finish; the host is drained when the count reads 0; ``undrain`` returns it. Every
result carries a status line that says whether the host is safe to power down: an unreadable lane count is never
safe. Every command is one chain under one correlation id: a start, then a success or a typed failure.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    EnumDrainOp,
    EnumDrainOutcome,
    ModelHostDrainRequest,
    ModelHostDrainResult,
)
from omnimarket.nodes.node_host_drain_effect.overlay import ModelDrainSettings
from omnimarket.nodes.node_host_drain_effect.protocols import (
    ProtocolDrainStore,
    ProtocolLaneSource,
)


def _format_until(until: datetime) -> str:
    """The mark's expiry as YYYY-MM-DDTHH:MM:SSZ; a naive stamp reads as UTC."""
    stamp = until if until.tzinfo is not None else until.replace(tzinfo=UTC)
    return stamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lanes_text(running: int | None, scope: str) -> str:
    """The lane count and where it was read: a count read on one machine is never the whole lab's."""
    if running is None:
        return "lane count unreadable"
    return f"{running} lane(s) running ({scope})"


class HandlerHostDrain:
    def __init__(
        self,
        settings: ModelDrainSettings,
        *,
        store: ProtocolDrainStore,
        lanes: ProtocolLaneSource,
        sink: ProtocolEmissionSink,
        clock: Callable[[], datetime],
        backend: Literal["in_memory", "real"],
    ) -> None:
        self._settings = settings
        self._store = store
        self._lanes = lanes
        self._sink = sink
        self._clock = clock
        self._backend = backend

    def handle(self, request: ModelHostDrainRequest) -> ModelHostDrainResult:
        cid = request.correlation_id or uuid4().hex
        codes = self._settings.codes
        chain = ChainEmitter(
            self._sink,
            node=self._settings.chain_node,
            piece=self._settings.piece,
            clock=lambda: self._clock().astimezone(UTC),
            correlation_id=cid,
            backend=self._backend,
            origin="real",
        )
        chain.start()
        host = request.host_name
        if host not in self._settings.hosts:
            return self._failed(
                chain, host, cid, codes.unknown_host, f"{host} is not a declared host"
            )
        if request.op is EnumDrainOp.CHECK_START:
            return self._check_start(chain, host, cid)
        if request.op in (EnumDrainOp.DRAIN, EnumDrainOp.UNDRAIN):
            want = request.op is EnumDrainOp.DRAIN
            # A drain mark is time-bounded: the ttl the command names or the overlay default, never above the max.
            # The ttl is checked before any write, so a refused drain leaves no mark.
            until: datetime | None = None
            if want:
                ttl = request.ttl_seconds or self._settings.default_ttl_seconds
                if ttl > self._settings.max_ttl_seconds:
                    return self._failed(
                        chain,
                        host,
                        cid,
                        codes.ttl_invalid,
                        f"a drain of {host} for {ttl}s is above the longest ttl the overlay "
                        f"allows ({self._settings.max_ttl_seconds}s): no mark is written",
                    )
                until = self._clock() + timedelta(seconds=ttl)
            try:
                self._store.set_draining(
                    host, want, reason=request.reason, at=self._clock(), until=until
                )
            except (
                Exception
            ) as exc:  # any backend failure ends the chain with a typed failure
                return self._failed(
                    chain,
                    host,
                    cid,
                    codes.write_failed,
                    f"the drain store did not take the write: {type(exc).__name__}",
                )
            readback = self._read_draining(host)
            if readback is None:
                return self._failed(
                    chain,
                    host,
                    cid,
                    codes.readback_failed,
                    f"the write to the drain store returned, but its readback for {host} failed: "
                    "the state is changed or not, unverified",
                )
            if readback is not want:
                return self._failed(
                    chain,
                    host,
                    cid,
                    codes.readback_failed,
                    f"the drain store does not read {host} as "
                    f"{'draining' if want else 'placeable'} after the write",
                )
        draining = self._read_draining(host)
        if draining is None:
            return self._failed(
                chain,
                host,
                cid,
                codes.readback_failed,
                f"the drain store could not be read for {host}",
            )
        running, detail = self._count(host)
        until = self._read_until(host) if draining else None
        outcome = {
            EnumDrainOp.DRAIN: EnumDrainOutcome.DRAINING,
            EnumDrainOp.UNDRAIN: EnumDrainOutcome.CLEARED,
            EnumDrainOp.STATUS: EnumDrainOutcome.STATUS_READ,
        }[request.op]
        # An unreadable count is neither published nor returned as a measured zero.
        counters = {"draining": int(draining)}
        if running is None:
            counters["lanes_unreadable"] = 1
        else:
            counters["lanes_running"] = running
        chain.success(counters)
        return ModelHostDrainResult(
            outcome=outcome,
            host_name=host,
            correlation_id=cid,
            draining=draining,
            lanes_running=running,
            drained=draining and running == 0,
            until=until,
            status_line=self._status_line(host, draining, until, running),
            detail=detail,
        )

    def _read_draining(self, host: str) -> bool | None:
        """Whether the store reads the host as draining; None when the store could not be read."""
        try:
            return self._store.is_draining(host)
        except Exception:  # a backend failure is an unreadable state, not an escape
            return None

    def _read_until(self, host: str) -> datetime | None:
        """When the host's drain mark expires; None when the store gives none or cannot be read."""
        try:
            return self._store.until_of(host)
        except (
            Exception
        ):  # the mark's expiry is read with the mark; an unreadable one reads as none
            return None

    def _status_line(
        self,
        host: str,
        draining: bool,
        until: datetime | None,
        running: int | None,
    ) -> str:
        """Whether the host is safe to power down, in one line. An unreadable count is never safe."""
        if not draining:
            return f"{host} not draining: {_lanes_text(running, self._settings.lane_count_scope)}"
        when = "unknown" if until is None else _format_until(until)
        if running is None:
            return (
                f"{host} draining until {when}: lane count unreadable: "
                "NOT SAFE to power down"
            )
        safety = "SAFE" if running == 0 else "NOT SAFE"
        return (
            f"{host} draining until {when}: {_lanes_text(running, self._settings.lane_count_scope)}: "
            f"{safety} to power down"
        )

    def _check_start(
        self, chain: ChainEmitter, host: str, cid: str
    ) -> ModelHostDrainResult:
        draining = self._read_draining(host)
        if draining is None:
            # A start is admitted only on a state that was read: an unreadable store fails closed.
            return self._failed(
                chain,
                host,
                cid,
                self._settings.codes.readback_failed,
                f"the drain store could not be read for {host}: no start is admitted",
            )
        if draining:
            code = self._settings.codes.host_draining
            detail = f"{host} is draining: no new lane is placed on it"
            chain.failure(code, detail)
            return ModelHostDrainResult(
                outcome=EnumDrainOutcome.START_REFUSED,
                host_name=host,
                correlation_id=cid,
                draining=True,
                admitted=False,
                failure_code=code,
                detail=detail,
            )
        chain.success({"admitted": 1})
        return ModelHostDrainResult(
            outcome=EnumDrainOutcome.START_ADMITTED,
            host_name=host,
            correlation_id=cid,
            draining=False,
            admitted=True,
        )

    def _count(self, host: str) -> tuple[int | None, str]:
        try:
            return self._lanes.running(host), ""
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return None, f"lane count unreadable: {type(exc).__name__}"

    @staticmethod
    def _failed(
        chain: ChainEmitter, host: str, cid: str, code: str, detail: str
    ) -> ModelHostDrainResult:
        chain.failure(code, detail)
        return ModelHostDrainResult(
            outcome=EnumDrainOutcome.FAILED,
            host_name=host,
            correlation_id=cid,
            failure_code=code,
            detail=detail,
        )
