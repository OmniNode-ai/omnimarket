# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The drain verb (rebuild plan 7.4, OMN-22905): a host leaves placement by a command, not by a data pull request.

``drain`` sets the host draining in the drain store and reads it back; the mark is time-bounded, so the host reads
not draining again once its ttl is past (OMN-24489); ``check_start`` refuses a new lane for a draining host; running
lanes are left to finish; ``undrain`` returns the host. Every result carries a status line that says whether the host
is safe to power down, and SAFE is a reading of the host itself: the overlay's host reads (its lanes, its units, its
model servers) and this machine's runner receipts all read 0, twice, on a mark that outlives the process. Anything
unread, any count above 0, a mark that dies with the process or a host with no host read declared is NOT SAFE.
Every command is one chain under one correlation id: a start, then a success or a typed failure.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    EnumDrainOp,
    EnumDrainOutcome,
    ModelDrainMark,
    ModelHostDrainRequest,
    ModelHostDrainResult,
    ModelHostReadResult,
)
from omnimarket.nodes.node_host_drain_effect.overlay import ModelDrainSettings
from omnimarket.nodes.node_host_drain_effect.protocols import (
    ProtocolDrainStore,
    ProtocolHostRead,
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


def _reads_text(reads: Sequence[ModelHostReadResult]) -> str:
    """The host reads in one phrase: ``lanes=0 units=0 model_servers=unreadable``; ``none declared`` when empty."""
    if not reads:
        return "host read none declared"
    return "host read " + " ".join(
        f"{r.kind.value}={'unreadable' if r.count is None else r.count}" for r in reads
    )


@dataclass(frozen=True)
class _Reading:
    """What one pass over the host showed: the receipts count and each host read, None where it could not be read."""

    receipts: int | None
    receipts_detail: str
    reads: tuple[ModelHostReadResult, ...]

    def merged(self, later: _Reading) -> _Reading:
        """Two passes in one: a count unread in either pass is unread, else the larger of the two (a lane that
        appeared between the passes shows)."""

        def pick(a: int | None, b: int | None) -> int | None:
            return None if a is None or b is None else max(a, b)

        return _Reading(
            receipts=pick(self.receipts, later.receipts),
            receipts_detail=self.receipts_detail or later.receipts_detail,
            reads=tuple(
                ModelHostReadResult(
                    name=a.name, kind=a.kind, count=pick(a.count, b.count)
                )
                for a, b in zip(self.reads, later.reads, strict=True)
            ),
        )

    @property
    def idle(self) -> bool:
        """Every count was read and is 0."""
        return self.receipts == 0 and all(r.count == 0 for r in self.reads)


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
        host_reads: Sequence[ProtocolHostRead] = (),
    ) -> None:
        self._settings = settings
        self._store = store
        self._host_reads = tuple(host_reads)
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
        # One status is one clock, one mark and two passes over the host: the line it prints is one moment's reading.
        now = self._clock()
        first = self._read_host(host)
        mark_read, mark = self._read_mark(host)
        if not mark_read:
            return self._failed(
                chain,
                host,
                cid,
                codes.readback_failed,
                f"the drain store could not be read for {host}",
            )
        draining = mark is not None and mark.reads_draining(now)
        until = mark.until if draining and mark is not None else None
        reading = first.merged(self._read_host(host))
        reasons = self._not_safe_reasons(draining, reading)
        safe = draining and not reasons
        outcome = {
            EnumDrainOp.DRAIN: EnumDrainOutcome.DRAINING,
            EnumDrainOp.UNDRAIN: EnumDrainOutcome.CLEARED,
            EnumDrainOp.STATUS: EnumDrainOutcome.STATUS_READ,
        }[request.op]
        # An unreadable count is neither published nor returned as a measured zero.
        counters = {"draining": int(draining)}
        if reading.receipts is None:
            counters["lanes_unreadable"] = 1
        else:
            counters["lanes_running"] = reading.receipts
        chain.success(counters)
        return ModelHostDrainResult(
            outcome=outcome,
            host_name=host,
            correlation_id=cid,
            draining=draining,
            lanes_running=reading.receipts,
            drained=safe,
            safe=safe,
            host_reads=reading.reads,
            until=until,
            status_line=self._status_line(host, draining, until, reading, reasons),
            detail="; ".join(
                part for part in (reading.receipts_detail, *reasons) if part
            ),
        )

    def _read_host(self, host: str) -> _Reading:
        """One pass: this machine's receipts count and each host read; an unreadable one is None, never 0."""
        receipts, receipts_detail = self._count(host)
        reads: list[ModelHostReadResult] = []
        for read in self._host_reads:
            try:
                count: int | None = read.read(host)
            except (OSError, ValueError, subprocess.SubprocessError):
                count = None
            reads.append(
                ModelHostReadResult(
                    name=read.name,
                    kind=read.kind,
                    count=count if count is None or count >= 0 else None,
                )
            )
        return _Reading(
            receipts=receipts, receipts_detail=receipts_detail, reads=tuple(reads)
        )

    def _not_safe_reasons(self, draining: bool, reading: _Reading) -> tuple[str, ...]:
        """Why the host is not safe to power down; empty when it is. Everything unread or non-zero is a reason."""
        reasons: list[str] = []
        if not draining:
            return ("the host is not draining",)
        if not self._store.durable:
            reasons.append(
                "the drain mark is held in memory only and is gone when this process ends"
            )
        if not self._host_reads:
            reasons.append("no host read is declared: the host itself was not read")
        if reading.receipts is None:
            reasons.append("the runner receipts could not be read")
        elif reading.receipts > 0:
            reasons.append(f"{reading.receipts} lane receipt(s) not final")
        for read in reading.reads:
            if read.count is None:
                reasons.append(f"the host read {read.name} could not be read")
            elif read.count > 0:
                reasons.append(f"the host read {read.name} counts {read.count}")
        return tuple(reasons)

    def _read_mark(self, host: str) -> tuple[bool, ModelDrainMark | None]:
        """Whether the store could be read, and the host's raw record (None: the host has none)."""
        try:
            return True, self._store.mark_of(host)
        except Exception:  # a backend failure is an unreadable state, not an escape
            return False, None

    def _read_draining(self, host: str) -> bool | None:
        """Whether the store reads the host as draining; None when the store could not be read."""
        try:
            return self._store.is_draining(host)
        except Exception:  # a backend failure is an unreadable state, not an escape
            return None

    def _status_line(
        self,
        host: str,
        draining: bool,
        until: datetime | None,
        reading: _Reading,
        reasons: tuple[str, ...],
    ) -> str:
        """Whether the host is safe to power down, in one line. Anything unread is never safe."""
        counts = (
            f"{_lanes_text(reading.receipts, self._settings.lane_count_scope)}; "
            f"{_reads_text(reading.reads)}"
        )
        if not draining:
            return f"{host} not draining: {counts}"
        when = "no expiry" if until is None else _format_until(until)
        safety = "NOT SAFE" if reasons else "SAFE"
        return f"{host} draining until {when}: {counts}: {safety} to power down"

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
