# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Landing tick orchestrator: one tick through the node path, compared per pull request (OMN-22921).

handle(ModelTickRequest) -> ModelTickResult. The decider computes the tick's decision from the
facts, the reducer node folds it per pull request and names each one that differs from the
decision the script recorded, and one event goes out on the contract's topic: compared, or a typed
failure. Replay and observe never write. Act is refused unless the overlay enables it, and then
only an injected writer is ever called; the script keeps landing until the node reads true too.

A landing snapshot step record carries no recorded decision. For a cutover repository the old
controller rules out of scope (it reads no facts and decides nothing for it), act mode writes that
record itself, no action, and compares that repository's subjects only; any other tick with no
recorded decision is refused RECORDED_DECISION_MISSING (OMN-23458). When the record instead
carries the old controller's finished tick (its decision rows, never its facts), the compare step
uses that controller's real decision for the tick, in scope, instead of a synthetic no action
(OMN-23665). An observe-mode record with neither is unread, CONTROLLER_TICK_MISSING: observe mode
never assumes a decision (OMN-22921).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.models.landing_decision import (
    ModelLandingAction,
    ModelLandingControllerState,
    ModelLandingDecision,
)
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
        cutover_repository: str | None = None,
        script_out_of_scope: tuple[str, ...] = (),
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
        self._cutover_repository = cutover_repository
        self._script_out_of_scope = frozenset(script_out_of_scope)
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
                        "recorded_written": int(result.recorded_written),
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
            origin="real",
        )

    def _compare(self, request: ModelTickRequest) -> ModelTickResult:
        if request.mode is EnumTickMode.ACT and not self._act_enabled:
            return self._failed(
                request,
                EnumTickFailureReason.ACT_NOT_ENABLED,
                "the overlay does not enable act mode",
            )
        if request.controller is not None and request.mode in (
            EnumTickMode.OBSERVE,
            EnumTickMode.REPLAY,
            EnumTickMode.ACT,
        ):
            try:
                decision = self._decider.decide(request.facts, request.fair_share)
            except Exception as exc:
                return self._failed(
                    request, EnumTickFailureReason.DECISION_RAISED, type(exc).__name__
                )
            scope = self._cutover_repository
            node_projected = self._projected(decision, scope, request.facts.tick)
            # the controller's real decision for the tick: its decision rows, in scope, as the
            # carry holds them (it carries no facts to recompute them from)
            controller_projected = node_projected.model_copy(
                update={
                    "actions": tuple(
                        ModelLandingAction.model_construct(
                            kind=row.kind, subject=row.subject
                        )
                        for row in request.controller.decisions
                        if scope is None or row.subject.partition("#")[0] == scope
                    )
                }
            )
            reduced = self._reducer.handle(
                ModelTickReduceInput(
                    decision=node_projected, recorded=controller_projected
                )
            )
            divergent_subjects = {d.subject for d in reduced.divergences}
            compared_prs = tuple(
                sorted(
                    {
                        pr.pr
                        for pr in request.facts.prs
                        if scope is None or pr.pr.partition("#")[0] == scope
                    }
                )
            )
            agreed_prs = tuple(
                pr for pr in compared_prs if pr not in divergent_subjects
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
                recorded_actions=len(controller_projected.actions),
                writes_performed=writes,
                recorded_written=False,
                controller_tick=request.controller.controller_tick,
                compared_prs=compared_prs,
                agreed_prs=agreed_prs,
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
                    controller_tick=request.controller.controller_tick,
                    compared_prs=len(compared_prs),
                    agreed_prs=len(agreed_prs),
                ),
            )
            return result
        recorded = request.recorded_decision
        only: str | None = None
        if recorded is None:
            only = self._written_record_repository(request)
            if only is None:
                return self._failed(
                    request,
                    EnumTickFailureReason.CONTROLLER_TICK_MISSING
                    if request.mode is EnumTickMode.OBSERVE
                    else EnumTickFailureReason.RECORDED_DECISION_MISSING,
                    self._missing_detail(request),
                )
        try:
            decision = self._decider.decide(request.facts, request.fair_share)
        except Exception as exc:
            return self._failed(
                request, EnumTickFailureReason.DECISION_RAISED, type(exc).__name__
            )
        if recorded is None:
            decision = decision.model_copy(
                update={
                    "actions": tuple(
                        a
                        for a in decision.actions
                        if a.subject.partition("#")[0] == only
                    )
                }
            )
            # the old controller reads no facts and decides nothing for a repository it rules
            # out of scope: its recorded decision for that repository is no action
            recorded = decision.model_copy(update={"actions": ()})
        reduced = self._reducer.handle(
            ModelTickReduceInput(decision=decision, recorded=recorded)
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
            recorded_actions=len(recorded.actions),
            writes_performed=writes,
            recorded_written=only is not None,
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

    def _projected(
        self,
        decision: ModelLandingDecision,
        scope: str | None,
        tick: int,
    ) -> ModelLandingDecision:
        """The decision's comparison projection (OMN-23665): its in-scope actions, stripped.

        Only the actions whose subject's repository is in scope carry through, each stripped of
        the brief, lease, attempt and owner state the controller's log does not carry; every other
        section (and the next state) is emptied and the tick is set on both sides, so the two projections compare on
        their actions. model_copy runs no validators, so a dispatch worker without its brief
        is fine.
        """
        kept = tuple(
            action.model_copy(
                update={
                    "brief": None,
                    "cause_brief": None,
                    "lease_id": None,
                    "attempts": (),
                    "members": (),
                    "owner": None,
                    "dedupe_key": None,
                    "head_sha": None,
                }
            )
            for action in decision.actions
            if scope is None or action.subject.partition("#")[0] == scope
        )
        return decision.model_copy(
            update={
                "tick": tick,
                "actions": kept,
                "observed_actions": (),
                "recorded_outcomes": (),
                "degraded": (),
                "violations": (),
                "companion_verdicts": (),
                "observe_only_refused": (),
                "gates": (),
                "land_skips": (),
                "liveness_unread": (),
                "next_state": ModelLandingControllerState(),
            }
        )

    def _written_record_repository(self, request: ModelTickRequest) -> str | None:
        """The repository whose recorded decision act mode writes, or None when it may not.

        Only act mode writes it (OMN-23458): the snapshot step's records carry no recorded decision,
        and the old controller's decision for a repository it rules out of scope is no action. Observe
        mode never assumes a decision: a record with no controller tick is unread
        (controller_tick_missing, OMN-22921), because the controller does decide the cutover
        repository, so an assumed no action there compared the node with a decision nobody made.
        """
        if (
            request.mode is EnumTickMode.ACT
            and self._cutover_repository is not None
            and self._cutover_repository in self._script_out_of_scope
        ):
            return self._cutover_repository
        return None

    def _missing_detail(self, request: ModelTickRequest) -> str:
        if request.mode is EnumTickMode.OBSERVE:
            return (
                "the record carries no controller tick, so there is no decision of the old "
                "controller to compare with; none is assumed"
            )
        if request.mode is not EnumTickMode.ACT:
            return "no recorded decision to compare with"
        if self._cutover_repository is None:
            return "no recorded decision to compare with, and act mode names no cutover repository"
        return (
            f"no recorded decision to compare with, and the cutover repository "
            f"{self._cutover_repository} is not one the old controller rules out of scope, "
            "so its decision must be recorded"
        )


__all__: list[str] = [
    "HandlerPrLandingTickOrchestrator",
    "contract_topics",
    "node_name",
]
