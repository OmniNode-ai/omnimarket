# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The drain verb (rebuild plan 7.4, OMN-22905): a host leaves placement by a command, not by a data pull request.

``drain`` sets the host draining in the drain store and reads it back; ``check_start`` refuses a new lane for a draining
host; running lanes are counted and left to finish; the host is drained when the count reads 0; ``undrain`` returns it.
Every command is one chain under one correlation id: a start, then a success or a typed failure.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
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
            self._store.set_draining(
                host, want, reason=request.reason, at=self._clock()
            )
            if self._store.is_draining(host) is not want:
                return self._failed(
                    chain,
                    host,
                    cid,
                    codes.readback_failed,
                    f"the drain store does not read {host} as "
                    f"{'draining' if want else 'placeable'} after the write",
                )
        draining = self._store.is_draining(host)
        running, detail = self._count(host)
        outcome = {
            EnumDrainOp.DRAIN: EnumDrainOutcome.DRAINING,
            EnumDrainOp.UNDRAIN: EnumDrainOutcome.CLEARED,
            EnumDrainOp.STATUS: EnumDrainOutcome.STATUS_READ,
        }[request.op]
        chain.success({"lanes_running": max(running, 0), "draining": int(draining)})
        return ModelHostDrainResult(
            outcome=outcome,
            host_name=host,
            correlation_id=cid,
            draining=draining,
            lanes_running=max(running, 0),
            drained=draining and running == 0,
            detail=detail,
        )

    def _check_start(
        self, chain: ChainEmitter, host: str, cid: str
    ) -> ModelHostDrainResult:
        if self._store.is_draining(host):
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

    def _count(self, host: str) -> tuple[int, str]:
        try:
            return self._lanes.running(host), ""
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return -1, f"lane count unreadable: {type(exc).__name__}"

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
