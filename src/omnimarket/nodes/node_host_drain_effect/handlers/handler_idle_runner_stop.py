# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The idle-only runner stop (rebuild plan 7.4, OMN-22905): a runner that holds a job is never stopped.

The runner's activity is read first. Idle: the stop runs and the runner is read again to confirm it is down. Busy or
unreadable: the stop is refused and nothing is stopped (fail closed). Every call is one chain under one correlation id.
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
        sink: ProtocolEmissionSink,
        clock: Callable[[], datetime],
        backend: Literal["in_memory", "real"],
    ) -> None:
        self._settings = settings
        self._probe = probe
        self._stopper = stopper
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
        activity = self._probe.activity(name)
        if activity is EnumRunnerActivity.BUSY:
            return self._end(
                chain, name, cid, EnumRunnerStopOutcome.REFUSED_BUSY, activity,
                codes.runner_busy, f"{name} holds a job: not stopped",
            )  # fmt: skip
        if activity is not EnumRunnerActivity.IDLE:
            return self._end(
                chain, name, cid, EnumRunnerStopOutcome.REFUSED_UNREADABLE, activity,
                codes.runner_unreadable, f"{name} cannot be read: not stopped",
            )  # fmt: skip
        if not self._stopper.stop(name):
            return self._end(
                chain, name, cid, EnumRunnerStopOutcome.FAILED, activity,
                codes.stop_failed, f"the stop command for {name} did not exit 0",
            )  # fmt: skip
        if self._probe.is_running(name) is not False:
            return self._end(
                chain, name, cid, EnumRunnerStopOutcome.FAILED, activity,
                codes.still_running, f"{name} still reads as running after the stop",
            )  # fmt: skip
        chain.success({"stopped": 1})
        return ModelIdleRunnerStopResult(
            outcome=EnumRunnerStopOutcome.STOPPED,
            runner_name=name,
            correlation_id=cid,
            activity=activity,
        )

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
