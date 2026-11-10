# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHostBringupPlan: the pure host bring-up plan over the declared target graph (OMN-21883).

Pure and deterministic: no clock, no file, no network, no shell. The caller supplies
the target rows (loaded from the bring-up orchestrator contract with
``load_bringup_graph``) and the results of each target's read-only check.

The plan is refused, naming what is wrong, when:

* a target name is declared twice;
* a ``needs`` entry names no target (the refusal names the target and the need);
* the needs contain a cycle (the refusal names the cycle, rotated to start at its
  smallest name, e.g. ``a -> b -> a``);
* no target is marked top, or a target no top target reaches through needs;
* an observation names no target.

Otherwise every target becomes one step, needs before the targets that need them,
ties broken by stage then name, so the same inputs give byte-equal output whatever
order the rows or observations arrive in. A step is skipped only when its check
observed ``ok``; an ``unknown`` result, a missing observation or two observations
that disagree all act, whatever the check kind.
"""

from __future__ import annotations

import heapq
from collections.abc import Mapping

from omnimarket.nodes.node_host_bringup_plan_compute.models.model_host_bringup_plan import (
    EnumObservedState,
    EnumPlanStatus,
    EnumRefusalKind,
    EnumStepDecision,
    ModelBringupGraph,
    ModelBringupTarget,
    ModelCheckObservation,
    ModelHostBringupPlan,
    ModelHostBringupPlanRequest,
    ModelPlanRefusal,
    ModelPlanStep,
)


def load_bringup_graph(contract: Mapping[str, object]) -> ModelBringupGraph:
    """Validate a contract's ``targets`` rows into the typed graph.

    A row missing its check, falsifier or action, or carrying an unknown key or
    an action outside the enum, raises ``pydantic.ValidationError``.
    """
    return ModelBringupGraph.model_validate({"targets": contract.get("targets")})


def _refusal(
    kind: EnumRefusalKind, names: tuple[str, ...], message: str
) -> ModelPlanRefusal:
    return ModelPlanRefusal(kind=kind, names=names, message=message)


def _canonical_cycle(path: list[str]) -> tuple[str, ...]:
    """Rotate a closed path (first == last) to start at its smallest name."""
    core = path[:-1]
    start = core.index(min(core))
    rotated = core[start:] + core[:start]
    return (*rotated, rotated[0])


def _find_cycles(edges: Mapping[str, tuple[str, ...]]) -> list[tuple[str, ...]]:
    """Every cycle closed by a back edge of a sorted depth-first walk, canonical and sorted."""
    found: set[tuple[str, ...]] = set()
    done: set[str] = set()
    for root in sorted(edges):
        if root in done:
            continue
        stack: list[str] = [root]
        on_stack: set[str] = {root}
        iters = [iter(edges[root])]
        while iters:
            node = stack[-1]
            nxt = next(iters[-1], None)
            if nxt is None:
                iters.pop()
                on_stack.discard(stack.pop())
                done.add(node)
                continue
            if nxt in on_stack:
                found.add(_canonical_cycle([*stack[stack.index(nxt) :], nxt]))
            elif nxt not in done:
                stack.append(nxt)
                on_stack.add(nxt)
                iters.append(iter(edges[nxt]))
    return sorted(found)


def _observed(
    observations: tuple[ModelCheckObservation, ...],
) -> dict[str, EnumObservedState]:
    seen: dict[str, set[EnumObservedState]] = {}
    for obs in observations:
        seen.setdefault(obs.target, set()).add(obs.state)
    return {
        name: states.pop() if len(states) == 1 else EnumObservedState.UNKNOWN
        for name, states in seen.items()
    }


class HandlerHostBringupPlan:
    """Fold the declared target graph and the check results into an ordered plan."""

    def handle(self, request: ModelHostBringupPlanRequest) -> ModelHostBringupPlan:
        targets = request.graph.targets
        refusals: list[ModelPlanRefusal] = []

        by_name: dict[str, ModelBringupTarget] = {}
        dupes: set[str] = set()
        for target in targets:
            if target.name in by_name:
                dupes.add(target.name)
            else:
                by_name[target.name] = target
        for name in sorted(dupes):
            refusals.append(
                _refusal(
                    EnumRefusalKind.DUPLICATE_TARGET,
                    (name,),
                    f"target {name} is declared more than once",
                )
            )

        edges: dict[str, tuple[str, ...]] = {}
        for name in sorted(by_name):
            needs = sorted(set(by_name[name].needs))
            for need in needs:
                if need not in by_name:
                    refusals.append(
                        _refusal(
                            EnumRefusalKind.UNKNOWN_NEED,
                            (name, need),
                            f"target {name} needs {need}, which names no target",
                        )
                    )
            edges[name] = tuple(n for n in needs if n in by_name)

        for cycle in _find_cycles(edges):
            refusals.append(
                _refusal(
                    EnumRefusalKind.CYCLE,
                    cycle,
                    "needs form a cycle: " + " -> ".join(cycle),
                )
            )

        tops = sorted(n for n, t in by_name.items() if t.top)
        if not tops:
            refusals.append(
                _refusal(
                    EnumRefusalKind.NO_TOP_TARGET,
                    (),
                    "no target is marked top, so nothing roots the graph",
                )
            )
        else:
            reached: set[str] = set()
            pending = list(tops)
            while pending:
                node = pending.pop()
                if node not in reached:
                    reached.add(node)
                    pending.extend(edges[node])
            for name in sorted(set(by_name) - reached):
                refusals.append(
                    _refusal(
                        EnumRefusalKind.UNREACHED_TARGET,
                        (name,),
                        f"target {name} is reached by no top target ({', '.join(tops)})",
                    )
                )

        observed = _observed(request.observations)
        for name in sorted(set(observed) - set(by_name)):
            refusals.append(
                _refusal(
                    EnumRefusalKind.UNKNOWN_OBSERVED_TARGET,
                    (name,),
                    f"an observation names {name}, which names no target",
                )
            )

        if refusals:
            return ModelHostBringupPlan(
                status=EnumPlanStatus.REFUSED, refusals=tuple(refusals)
            )

        return ModelHostBringupPlan(
            status=EnumPlanStatus.PLANNED,
            steps=self._steps(by_name, edges, observed),
        )

    @staticmethod
    def _steps(
        by_name: Mapping[str, ModelBringupTarget],
        edges: Mapping[str, tuple[str, ...]],
        observed: Mapping[str, EnumObservedState],
    ) -> tuple[ModelPlanStep, ...]:
        waiting = {name: len(needs) for name, needs in edges.items()}
        needed_by: dict[str, list[str]] = {name: [] for name in edges}
        for name, needs in edges.items():
            for need in needs:
                needed_by[need].append(name)
        ready = [(by_name[n].stage, n) for n, count in waiting.items() if count == 0]
        heapq.heapify(ready)
        steps: list[ModelPlanStep] = []
        while ready:
            _, name = heapq.heappop(ready)
            target = by_name[name]
            state = observed.get(name, EnumObservedState.UNKNOWN)
            steps.append(
                ModelPlanStep(
                    order=len(steps) + 1,
                    target=name,
                    stage=target.stage,
                    check_kind=target.check.kind,
                    observed=state,
                    decision=(
                        EnumStepDecision.SKIP
                        if state is EnumObservedState.OK
                        else EnumStepDecision.ACT
                    ),
                    action=target.action,
                )
            )
            for dependant in needed_by[name]:
                waiting[dependant] -= 1
                if waiting[dependant] == 0:
                    heapq.heappush(ready, (by_name[dependant].stage, dependant))
        return tuple(steps)


__all__ = ["HandlerHostBringupPlan", "load_bringup_graph"]
