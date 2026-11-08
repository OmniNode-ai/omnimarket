# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The idle-only runner stop (rebuild plan 7.4, OMN-22905): a runner that holds a job is never stopped.

Admission is closed first: the runner's host is set draining and read back, so no start is admitted between the read
of the runner and its stop. The runner's activity is read next. Idle: the stop runs and the runner is read again to
confirm it is down; the host stays draining. Busy or unreadable, or a stop that did not take: nothing is left
stopped (fail closed) and the host returns to the state it had. Every call is one chain under one correlation id.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    EnumRunnerActivity,
    EnumRunnerStopOutcome,
    ModelIdleRunnerStopRequest,
    ModelIdleRunnerStopResult,
)
from omnimarket.nodes.node_host_drain_effect.overlay import ModelRunnerStopSettings
from omnimarket.nodes.node_host_drain_effect.protocols import (
    ProtocolDrainStore,
    ProtocolRunnerProbe,
    ProtocolRunnerStopper,
)


class HandlerIdleRunnerStop:
    def __init__(
        self,
        settings: ModelRunnerStopSettings,
        *,
        probe: ProtocolRunnerProbe,
        stopper: ProtocolRunnerStopper,
        store: ProtocolDrainStore,
        sink: ProtocolEmissionSink,
        clock: Callable[[], datetime],
        backend: Literal["in_memory", "real"],
    ) -> None:
        self._settings = settings
        self._probe = probe
        self._stopper = stopper
        self._store = store
        self._sink = sink
        self._clock = clock
        self._backend = backend

    def handle(self, request: ModelIdleRunnerStopRequest) -> ModelIdleRunnerStopResult:
        cid = request.correlation_id or uuid4().hex
        codes = self._settings.codes
        name = request.runner_name
        chain = ChainEmitter(
            self._sink,
            node=self._settings.chain_node,
            piece=self._settings.piece,
            clock=lambda: self._clock().astimezone(UTC),
            correlation_id=cid,
            backend=self._backend,
        )
        chain.start()
        if all(r.name != name for r in self._settings.runners):
            return self._end(
                chain, name, cid, EnumRunnerStopOutcome.FAILED,
                EnumRunnerActivity.UNKNOWN, codes.unknown_runner,
                f"{name} is not a declared runner",
            )  # fmt: skip
        host = next(r.host for r in self._settings.runners if r.name == name)
        prior = self._close_admission(host)
        if prior is None:
            return self._end(
                chain, name, cid, EnumRunnerStopOutcome.REFUSED_UNREADABLE,
                EnumRunnerActivity.UNKNOWN, codes.admission_not_closed,
                f"{host} could not be set draining and read back as draining: {name} not read, not stopped",
            )  # fmt: skip
        activity = self._probe.activity(name)
        if activity is EnumRunnerActivity.BUSY:
            return self._end(
                chain, name, cid, EnumRunnerStopOutcome.REFUSED_BUSY, activity,
                codes.runner_busy, f"{name} holds a job: not stopped" + self._reopen(host, prior),
            )  # fmt: skip
        if activity is not EnumRunnerActivity.IDLE:
            return self._end(
                chain, name, cid, EnumRunnerStopOutcome.REFUSED_UNREADABLE, activity,
                codes.runner_unreadable, f"{name} cannot be read: not stopped" + self._reopen(host, prior),
            )  # fmt: skip
        if not self._stopper.stop(name):
            return self._end(
                chain, name, cid, EnumRunnerStopOutcome.FAILED, activity,
                codes.stop_failed,
                f"the stop command for {name} did not exit 0" + self._reopen(host, prior),
            )  # fmt: skip
        if self._probe.is_running(name) is not False:
            return self._end(
                chain, name, cid, EnumRunnerStopOutcome.FAILED, activity,
                codes.still_running,
                f"{name} still reads as running after the stop" + self._reopen(host, prior),
            )  # fmt: skip
        chain.success({"stopped": 1})
        return ModelIdleRunnerStopResult(
            outcome=EnumRunnerStopOutcome.STOPPED,
            runner_name=name,
            correlation_id=cid,
            activity=activity,
        )

    def _close_admission(self, host: str) -> bool | None:
        """Set the host draining and read it back. Returns whether it was draining before, None when not closed."""
        try:
            prior = self._store.is_draining(host)
            if not prior:
                self._store.set_draining(
                    host, True, reason=self._settings.stop_reason, at=self._clock()
                )
            return prior if self._store.is_draining(host) else None
        except Exception:  # any store failure means admission is not known to be closed
            return None

    def _reopen(self, host: str, prior: bool) -> str:
        """Return the host to the state it had before the stop; the detail says when that did not take."""
        if prior:
            return ""
        try:
            self._store.set_draining(
                host, False, reason=self._settings.stop_reason, at=self._clock()
            )
            if not self._store.is_draining(host):
                return ""
        except Exception:  # reported in the detail, never raised over the typed result
            pass
        return f"; {host} could not be returned to placement: it still reads draining"

    @staticmethod
    def _end(
        chain: ChainEmitter,
        name: str,
        cid: str,
        outcome: EnumRunnerStopOutcome,
        activity: EnumRunnerActivity,
        code: str,
        detail: str,
    ) -> ModelIdleRunnerStopResult:
        chain.failure(code, detail)
        return ModelIdleRunnerStopResult(
            outcome=outcome,
            runner_name=name,
            correlation_id=cid,
            activity=activity,
            failure_code=code,
            detail=detail,
        )
