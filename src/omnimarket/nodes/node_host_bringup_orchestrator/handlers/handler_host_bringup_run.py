# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHostBringupRun: one run of the host bring-up graph (OMN-21899).

Holds the per-host lease, then loops: read every target's check in one pass, plan
with HandlerHostBringupPlan over those reads only, run the first acting step, read
that target again, emit one step event. It ends at the first of: nothing left to
act (exit 0), a wait on a person that times out or ``no_wait`` (75), a failed or
ineffective action or a ``stop`` row whose check reads unsatisfied (1), a fence a
successor replaced (3). ``up`` needs ``in-service``, which needs ``verify``, so
the host-row flip is always the last action of a run that reaches it.

The five conditions of the OMN-21882 design-review verdict, each held here:

1. Takeover closes every run id in the lease's owed set ``abandoned``, and only
   then removes it from the set.
2. Every path that ends a run emits its terminal through the bus's if-absent
   append keyed on the run id.
3. The terminal is emitted before the lease is released.
4. Every host action runs through ``lease.fenced``, which compares the fence and
   renews the lease to at least start plus ``lease_grace_s`` under the host lock
   before it runs; the request refuses a grace not above the action timeout.
5. A wait passes only on its check reading ok; at ``wait_timeout_s`` the run ends
   with the person line.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from functools import partial

from omnimarket.models.host_bringup import EnumBringupAction, EnumBringupCheckKind
from omnimarket.nodes.node_host_bringup_orchestrator.models.model_host_bringup_run import (
    EXIT_CODE,
    EnumRunOutcome,
    ModelActionResult,
    ModelBringupStepEvent,
    ModelBringupTerminalEvent,
    ModelHostBringupRunRequest,
    ModelHostBringupRunResult,
)
from omnimarket.nodes.node_host_bringup_orchestrator.protocols import (
    ProtocolBringupBus,
    ProtocolBringupLease,
    ProtocolClock,
    ProtocolTargetActuator,
    ProtocolTargetProbe,
)
from omnimarket.nodes.node_host_bringup_plan_compute.handlers.handler_host_bringup_plan import (
    HandlerHostBringupPlan,
)
from omnimarket.nodes.node_host_bringup_plan_compute.models.model_host_bringup_plan import (
    EnumObservedState,
    EnumPlanStatus,
    EnumStepDecision,
    ModelBringupGraph,
    ModelBringupTarget,
    ModelCheckObservation,
    ModelHostBringupPlanRequest,
)

_ENDS_RUN_ON_UNSATISFIED = frozenset(
    {EnumBringupAction.STOP, EnumBringupAction.NO_ACTION}
)


def _closure(by_name: Mapping[str, ModelBringupTarget], top: str) -> ModelBringupGraph:
    """The targets ``top`` reaches through needs, with ``top`` the only top."""
    reached: set[str] = set()
    pending = [top]
    while pending:
        name = pending.pop()
        if name in reached or name not in by_name:
            continue
        reached.add(name)
        pending.extend(by_name[name].needs)
    return ModelBringupGraph(
        targets=tuple(
            by_name[n].model_copy(update={"top": n == top}) for n in sorted(reached)
        )
    )


class HandlerHostBringupRun:
    """Drive one host toward one top target, one action per pass."""

    def __init__(
        self,
        *,
        probe: ProtocolTargetProbe,
        actuator: ProtocolTargetActuator,
        lease: ProtocolBringupLease,
        bus: ProtocolBringupBus,
        clock: ProtocolClock,
    ) -> None:
        self._probe = probe
        self._actuator = actuator
        self._lease = lease
        self._bus = bus
        self._clock = clock
        self._planner = HandlerHostBringupPlan()

    def handle(self, request: ModelHostBringupRunRequest) -> ModelHostBringupRunResult:
        by_name = {t.name: t for t in request.graph.targets}
        shape_error = self._graph_shape_error(request.graph, by_name)
        if shape_error is not None:
            return ModelHostBringupRunResult(
                run_id=request.run_id,
                host=request.host,
                fence=0,
                outcome=EnumRunOutcome.USAGE,
                exit_code=EXIT_CODE[EnumRunOutcome.USAGE],
                detail=shape_error,
            )
        if request.top_target not in by_name:
            return ModelHostBringupRunResult(
                run_id=request.run_id,
                host=request.host,
                fence=0,
                outcome=EnumRunOutcome.USAGE,
                exit_code=EXIT_CODE[EnumRunOutcome.USAGE],
                detail=f"target {request.top_target} names no declared target",
            )
        graph = _closure(by_name, request.top_target)

        grant = self._lease.acquire(
            request.host,
            request.run_id,
            self._clock.time() + request.lease_grace_s,
        )
        if grant is None:
            self._terminal(
                request, 0, EnumRunOutcome.LEASE_HELD, "another run holds the lease"
            )
            return self._result(
                request,
                0,
                EnumRunOutcome.LEASE_HELD,
                detail="another run holds the lease",
            )
        fence = grant.fence

        abandoned: list[str] = []
        for owed in grant.owed:
            if owed != request.run_id:
                self._bus.emit_terminal_if_absent(
                    ModelBringupTerminalEvent(
                        run_id=owed,
                        host=request.host,
                        fence=fence,
                        outcome=EnumRunOutcome.ABANDONED,
                        detail=f"closed by run {request.run_id} at takeover",
                        at=self._clock.time(),
                    )
                )
                abandoned.append(owed)
            self._lease.settle_owed(request.host, fence, owed)

        steps: list[ModelBringupStepEvent] = []

        def finish(
            outcome: EnumRunOutcome,
            *,
            detail: str = "",
            failed_target: str | None = None,
            person_line: str | None = None,
            remaining: tuple[str, ...] = (),
        ) -> ModelHostBringupRunResult:
            self._terminal(request, fence, outcome, detail)
            self._lease.release(request.host, fence)
            return self._result(
                request,
                fence,
                outcome,
                steps=tuple(steps),
                abandoned=tuple(abandoned),
                failed_target=failed_target,
                person_line=person_line,
                remaining=remaining,
                detail=detail,
            )

        for _ in range(request.max_passes):
            reads = self._read_pass(request.host, graph)
            plan = self._planner.handle(
                ModelHostBringupPlanRequest(
                    graph=graph,
                    observations=tuple(
                        ModelCheckObservation(target=n, state=s)
                        for n, s in sorted(reads.items())
                    ),
                )
            )
            if plan.status is EnumPlanStatus.REFUSED:
                return finish(
                    EnumRunOutcome.FAILED,
                    detail="; ".join(r.message for r in plan.refusals),
                )
            acting = [s for s in plan.steps if s.decision is EnumStepDecision.ACT]
            if not acting:
                return self._complete_if_held(request, fence, finish)
            remaining = tuple(s.target for s in acting)
            target = by_name[acting[0].target]

            if target.action is EnumBringupAction.PERSON_STEP:
                waited = self._wait(request, fence, target)
                if waited is None:
                    continue
                outcome, line = waited
                return finish(
                    outcome, detail=line, person_line=line, remaining=remaining
                )

            if target.action in _ENDS_RUN_ON_UNSATISFIED:
                return finish(
                    EnumRunOutcome.FAILED,
                    failed_target=target.name,
                    remaining=remaining,
                    detail=(
                        f"{target.name} reads {reads[target.name].value} and its action is "
                        f"{target.action.value}: {target.falsifier}"
                    ),
                )

            start = self._clock.time()
            act: Callable[[], ModelActionResult] = partial(
                self._act_bounded, request, target, fence
            )
            held, result = self._lease.fenced(
                request.host, fence, start + request.lease_grace_s, act
            )
            if not held or result is None:
                return finish(
                    EnumRunOutcome.REFUSED_STALE,
                    remaining=remaining,
                    detail=f"fence {fence} replaced before {target.name}",
                )
            if not result.ok:
                return finish(
                    EnumRunOutcome.FAILED,
                    failed_target=target.name,
                    remaining=remaining,
                    detail=result.detail or f"{target.name} action failed",
                )
            after = self._probe.read(request.host, target)
            event = ModelBringupStepEvent(
                run_id=request.run_id,
                host=request.host,
                fence=fence,
                target=target.name,
                action=target.action,
                before=reads[target.name],
                after=after,
                at=self._clock.time(),
            )
            self._bus.emit_step(event)
            steps.append(event)
            if after is not EnumObservedState.OK:
                return finish(
                    EnumRunOutcome.FAILED,
                    failed_target=target.name,
                    remaining=remaining,
                    detail=f"{target.name} action ran and its check reads {after.value}",
                )

        # The last pass's action has been probed ok but not read in a pass of its own: read once more before giving up.
        reads = self._read_pass(request.host, graph)
        closing = self._planner.handle(
            ModelHostBringupPlanRequest(
                graph=graph,
                observations=tuple(
                    ModelCheckObservation(target=n, state=s)
                    for n, s in sorted(reads.items())
                ),
            )
        )
        if closing.status is not EnumPlanStatus.REFUSED and not any(
            s.decision is EnumStepDecision.ACT for s in closing.steps
        ):
            return self._complete_if_held(request, fence, finish)
        return finish(
            EnumRunOutcome.FAILED,
            detail=f"max_passes {request.max_passes} reached with targets still acting",
        )

    def _complete_if_held(
        self,
        request: ModelHostBringupRunRequest,
        fence: int,
        finish: Callable[..., ModelHostBringupRunResult],
    ) -> ModelHostBringupRunResult:
        """COMPLETED only while this run still holds the lease: a replaced fence ends the run REFUSED_STALE."""
        held, _ = self._lease.fenced(
            request.host,
            fence,
            self._clock.time() + request.lease_grace_s,
            lambda: None,
        )
        if not held:
            return finish(
                EnumRunOutcome.REFUSED_STALE,
                detail=f"fence {fence} replaced before the run completed",
            )
        return finish(EnumRunOutcome.COMPLETED)

    def _act_bounded(
        self,
        request: ModelHostBringupRunRequest,
        target: ModelBringupTarget,
        fence: int,
    ) -> ModelActionResult:
        """Run the actuator on a daemon thread; one that does not return inside ``action_timeout_s`` is a failed action."""
        box: dict[str, ModelActionResult | BaseException] = {}

        def work() -> None:
            try:
                box["result"] = self._actuator.act(request.host, target, fence)
            except (
                BaseException
            ) as error:  # carried out of the thread and raised in the caller
                box["error"] = error

        worker = threading.Thread(target=work, name="host-bringup-act", daemon=True)
        worker.start()
        worker.join(request.action_timeout_s)
        if worker.is_alive():
            return ModelActionResult(
                ok=False,
                detail=f"{target.name} action did not return inside {request.action_timeout_s} s",
            )
        outcome = box.get("error") or box["result"]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    @staticmethod
    def _graph_shape_error(
        graph: ModelBringupGraph, by_name: Mapping[str, ModelBringupTarget]
    ) -> str | None:
        """Why the declared graph cannot be run as declared, or None. Judged on the request's own targets, before any closure."""
        names = [t.name for t in graph.targets]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            return f"target {', '.join(duplicates)} is declared more than once"
        undeclared = sorted(
            {n for t in graph.targets for n in t.needs if n not in by_name}
        )
        if undeclared:
            return f"a target needs {', '.join(undeclared)}, which names no declared target"
        return None

    def _read_pass(
        self, host: str, graph: ModelBringupGraph
    ) -> dict[str, EnumObservedState]:
        """Read every check once; an aggregate reads ok only when all its needs do."""
        by_name = {t.name: t for t in graph.targets}
        reads: dict[str, EnumObservedState] = {
            t.name: self._probe.read(host, t)
            for t in graph.targets
            if t.check.kind is not EnumBringupCheckKind.AGGREGATE
        }

        def aggregate(name: str) -> EnumObservedState:
            if name not in reads:
                needs = [aggregate(n) for n in by_name[name].needs]
                ok = all(s is EnumObservedState.OK for s in needs)
                reads[name] = EnumObservedState.OK if ok else EnumObservedState.NOT_OK
            return reads[name]

        for name in sorted(by_name):
            aggregate(name)
        return reads

    def _wait(
        self,
        request: ModelHostBringupRunRequest,
        fence: int,
        target: ModelBringupTarget,
    ) -> tuple[EnumRunOutcome, str] | None:
        """Poll a person step's check. None when it reads ok inside the timeout."""
        line = (
            f"{request.host}: {target.name} waits on a person "
            f"({target.check.kind.value} {target.check.subject}); "
            f"rerun `onex host up {request.host}` when done"
        )
        if request.no_wait:
            return EnumRunOutcome.WAITING, line
        deadline = self._clock.time() + request.wait_timeout_s
        while True:
            if self._probe.read(request.host, target) is EnumObservedState.OK:
                return None
            if self._clock.time() >= deadline:
                return EnumRunOutcome.WAITING, line
            # Never sleep past the deadline: a reading after it must not be accepted as inside the timeout.
            self._clock.sleep(
                max(0, min(request.poll_interval_s, deadline - self._clock.time()))
            )
            held, _ = self._lease.fenced(
                request.host,
                fence,
                self._clock.time() + request.lease_grace_s,
                lambda: None,
            )
            if not held:
                return (
                    EnumRunOutcome.REFUSED_STALE,
                    f"fence {fence} replaced during the wait on {target.name}",
                )

    def _terminal(
        self,
        request: ModelHostBringupRunRequest,
        fence: int,
        outcome: EnumRunOutcome,
        detail: str,
    ) -> None:
        self._bus.emit_terminal_if_absent(
            ModelBringupTerminalEvent(
                run_id=request.run_id,
                host=request.host,
                fence=fence,
                outcome=outcome,
                detail=detail,
                at=self._clock.time(),
            )
        )

    @staticmethod
    def _result(
        request: ModelHostBringupRunRequest,
        fence: int,
        outcome: EnumRunOutcome,
        *,
        steps: tuple[ModelBringupStepEvent, ...] = (),
        abandoned: tuple[str, ...] = (),
        failed_target: str | None = None,
        person_line: str | None = None,
        remaining: tuple[str, ...] = (),
        detail: str = "",
    ) -> ModelHostBringupRunResult:
        return ModelHostBringupRunResult(
            run_id=request.run_id,
            host=request.host,
            fence=fence,
            outcome=outcome,
            exit_code=EXIT_CODE[outcome],
            steps=steps,
            abandoned_run_ids=abandoned,
            failed_target=failed_target,
            person_line=person_line,
            remaining=remaining,
            detail=detail,
        )


__all__ = ["HandlerHostBringupRun"]
