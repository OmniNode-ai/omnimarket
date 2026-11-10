# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Landing tick orchestrator: one tick through the node path, compared per pull request (OMN-22921).

handle(ModelTickRequest) -> ModelTickResult. The decider computes the tick's decision from the
facts, the reducer node folds it per pull request and names each one that differs from the
decision the script recorded, and one event goes out on the contract's topic: compared, or a typed
failure. Replay and observe never write. Act is refused unless the overlay enables it, and then
only an injected writer is ever called; the script keeps landing until the node reads true too.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.models.landing_decision import ModelLandingDecision
from omnimarket.models.landing_tick import (
    ModelTickReduceInput,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    InMemoryTickPublisher,
    InMemoryTickWriter,
    NodePathDecider,
    ProtocolTickDecider,
    ProtocolTickPublisher,
    ProtocolTickWriter,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickFailureReason,
    EnumTickMode,
    EnumTickStatus,
    ModelTickComparedEvent,
    ModelTickFailedEvent,
    ModelTickRequest,
    ModelTickResult,
)
from omnimarket.nodes.node_pr_landing_tick_reducer.handlers.handler_pr_landing_tick_reducer import (
    HandlerPrLandingTickReducer,
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


def node_name() -> str:
    """The node's name as the contract declares it: what the floor's emission chains are filed under."""
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    return str(contract["name"])


def contract_topics() -> tuple[str, str]:
    """The compared and the failed topic, as the contract declares them."""
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    terminal = contract["runtime_dispatch"]["terminal_events"]
    return terminal["success"], terminal["failure"]


class HandlerPrLandingTickOrchestrator:
    """One tick: decide, reduce per pull request, publish. Writes only in an enabled act mode."""

    def __init__(
        self,
        *,
        decider: ProtocolTickDecider | None = None,
        reducer: HandlerPrLandingTickReducer | None = None,
        writer: ProtocolTickWriter | None = None,
        publisher: ProtocolTickPublisher | None = None,
        act_enabled: bool = False,
        emission_sink: ProtocolEmissionSink | None = None,
        floor_piece: int | None = None,
        emission_backend: str = "in_memory",
        drill: bool = False,
    ) -> None:
        self._decider: ProtocolTickDecider = decider or NodePathDecider()
        self._reducer = reducer or HandlerPrLandingTickReducer()
        self._writer: ProtocolTickWriter = writer or InMemoryTickWriter()
        self._publisher: ProtocolTickPublisher = publisher or InMemoryTickPublisher()
        self._act_enabled = act_enabled
        # The floor reads one chain per tick: a start, then a success or a typed failure.
        self._emission_sink = emission_sink
        self._floor_piece = floor_piece
        self._emission_backend = emission_backend
        self._drill = drill
        self._compared_topic, self._failed_topic = contract_topics()

    def _performed_in(
        self, request: ModelTickRequest, decision: ModelLandingDecision
    ) -> int:
        """Perform the decision's actions, in an enabled act mode only; otherwise none."""
        if request.mode is not EnumTickMode.ACT:
            return 0
        return sum(
            1
            for action in decision.actions
            if self._writer.perform(action, request.facts)
        )

    def _failed(
        self,
        request: ModelTickRequest,
        reason: EnumTickFailureReason,
        detail: str,
    ) -> ModelTickResult:
        self._publisher.publish(
            self._failed_topic,
            ModelTickFailedEvent(
                correlation_id=request.correlation_id,
                mode=request.mode,
                tick=request.facts.tick,
                reason=reason,
                detail=detail,
            ),
        )
        return ModelTickResult(
            correlation_id=request.correlation_id,
            mode=request.mode,
            status=EnumTickStatus.FAILED,
            tick=request.facts.tick,
            equal=False,
            failure_reason=reason,
            failure_detail=detail,
        )

    def handle(self, request: ModelTickRequest) -> ModelTickResult:
        chain = self._chain(request)
        if chain is not None:
            chain.start()
        result = self._compare(request)
        if chain is not None:
            if result.status is EnumTickStatus.COMPARED:
                chain.success(
                    {
                        "subjects": len(result.rows),
                        "divergent_subjects": len(result.divergences),
                        "node_actions": result.node_actions,
                        "writes_performed": result.writes_performed,
                    }
                )
            else:
                chain.failure(
                    result.failure_reason.value if result.failure_reason else "failed",
                    result.failure_detail,
                )
        return result

    def _chain(self, request: ModelTickRequest) -> ChainEmitter | None:
        if self._emission_sink is None or self._floor_piece is None:
            return None
        return ChainEmitter(
            self._emission_sink,
            node=node_name(),
            piece=self._floor_piece,
            correlation_id=request.correlation_id.hex,
            backend=self._emission_backend,
            drill=self._drill,
        )

    def _compare(self, request: ModelTickRequest) -> ModelTickResult:
        if request.mode is EnumTickMode.ACT and not self._act_enabled:
            return self._failed(
                request,
                EnumTickFailureReason.ACT_NOT_ENABLED,
                "the overlay does not enable act mode",
            )
        if request.recorded_decision is None:
            return self._failed(
                request,
                EnumTickFailureReason.RECORDED_DECISION_MISSING,
                "no recorded decision to compare with",
            )
        try:
            decision = self._decider.decide(request.facts, request.fair_share)
        except Exception as exc:
            return self._failed(
                request, EnumTickFailureReason.DECISION_RAISED, type(exc).__name__
            )
        reduced = self._reducer.handle(
            ModelTickReduceInput(decision=decision, recorded=request.recorded_decision)
        )
        writes = self._performed_in(request, decision)
        result = ModelTickResult(
            correlation_id=request.correlation_id,
            mode=request.mode,
            status=EnumTickStatus.COMPARED,
            tick=reduced.tick,
            equal=reduced.equal,
            rows=reduced.rows,
            divergences=reduced.divergences,
            node_actions=len(decision.actions),
            recorded_actions=len(request.recorded_decision.actions),
            writes_performed=writes,
        )
        self._publisher.publish(
            self._compared_topic,
            ModelTickComparedEvent(
                correlation_id=request.correlation_id,
                mode=request.mode,
                tick=reduced.tick,
                equal=reduced.equal,
                divergent_subjects=tuple(d.subject for d in reduced.divergences),
                node_actions=result.node_actions,
                recorded_actions=result.recorded_actions,
                writes_performed=writes,
            ),
        )
        return result


__all__: list[str] = [
    "HandlerPrLandingTickOrchestrator",
    "contract_topics",
    "node_name",
]
