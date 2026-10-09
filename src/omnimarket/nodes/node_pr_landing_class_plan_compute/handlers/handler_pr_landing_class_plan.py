# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The class actions a landing tick performs: stale CI Summary reruns, retargets, and what it remembers.

``HandlerPrLandingClassPlan.handle(ModelLandingClassPlanRequest) -> ModelLandingClassPlanResult``
(definition-B). The per-PR reading that finds a PR needing a class action is done before the tick's
decision; this plans which of those actions the tick performs inside what the quota governor's per-tick
cap left after the required-context reads, and defers the rest to the next tick:

* a PR the decision acts on itself this tick is left to that action;
* the stale reruns come first (by PR), then the retargets that are not waiting (by PR), and the M4
  delegation PRs move ahead of the others in that order (a stable sort);
* each action costs what ``CLASS_COST`` says, in the order above, and an action dearer than what is left
  is deferred while a cheaper one after it can still fit;
* every performed action is written to the PR's sidecar memory (the stale steps taken on the head, the
  retargets tried or the author asked), and the memory of a merged or closed PR is dropped.
"""

from __future__ import annotations

from datetime import UTC
from typing import Final

from omnimarket.nodes.node_pr_landing_class_plan_compute.models.model_landing_class_plan import (
    ClassStep,
    ModelLandingClassAction,
    ModelLandingClassPlanRequest,
    ModelLandingClassPlanResult,
    ModelLandingClassSummary,
    ModelLandingRetargetMemory,
    ModelLandingStaleMemory,
)

# What one class action costs in GitHub calls, charged to the quota governor's per-tick cap.
CLASS_COST: Final[dict[ClassStep, int]] = {
    "rerun": 2,
    "update_branch": 1,
    "retarget": 2,
    "comment": 1,
    "rerun_run": 2,
}
# The summary key of each step.
CLASS_KIND: Final[dict[ClassStep, str]] = {
    "rerun": "stale_rerun",
    "update_branch": "stale_refresh",
    "rerun_run": "stale_copy_rerun",
    "retarget": "retarget",
    "comment": "retarget_ask",
}
STALE_COPY_MEMO: Final[str] = (
    "rerun_run:"  # one superseded run's rerun, in the stale memory
)
TERMINAL_STATES: Final[frozenset[str]] = frozenset({"MERGED", "CLOSED"})
STAMP_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%SZ"


def short_key(subject: str) -> str:
    """The node's ``owner/repo#n`` to the watcher's ``repo#n``."""
    return subject.split("/", 1)[-1]


def stale_memory_next(
    mem: ModelLandingStaleMemory | None, head: str, step: str, *, now: str
) -> ModelLandingStaleMemory:
    """The sidecar's stale entry after ``step`` at ``head``; only the current head is kept."""
    m = mem or ModelLandingStaleMemory()
    done = (*m.heads.get(head, ()), step)
    refreshes = m.refreshes + (1 if step == "update_branch" else 0)
    return ModelLandingStaleMemory(heads={head: done}, refreshes=refreshes, at=now)


def retarget_memory_next(
    mem: ModelLandingRetargetMemory | None, head: str, step: str, *, now: str
) -> ModelLandingRetargetMemory:
    """The sidecar's retarget entry after ``step`` at ``head``."""
    m = mem or ModelLandingRetargetMemory()
    if step == "comment":
        return m.model_copy(update={"commented": True, "at": now})
    n = m.n if m.head == head else 0
    return m.model_copy(update={"head": head, "n": n + 1, "at": now})


def plan_landing_classes(
    request: ModelLandingClassPlanRequest,
) -> ModelLandingClassPlanResult:
    """Plan the tick's class actions inside the cap, and the memories they leave."""
    now = request.now.astimezone(UTC).strftime(STAMP_FORMAT)
    acted = {short_key(s) for s in request.acted if "#" in s}
    m4 = frozenset(request.m4)
    budget = max(0, request.class_cap - request.class_reads)
    runs = {e.short: e.run_id for e in request.stale}
    wanted: list[tuple[str, ClassStep, str]] = [
        (e.short, e.step, e.head) for e in sorted(request.stale, key=lambda e: e.short)
    ]
    wanted += [
        (e.short, e.step, e.head)
        for e in sorted(request.retarget, key=lambda e: e.short)
        if e.step != "wait"
    ]
    # the quota cap goes to the M4 class first (a stable sort)
    wanted.sort(key=lambda w: w[0] not in m4)
    stale_mem = dict(request.stale_memory)
    retarget_mem = dict(request.retarget_memory)
    plan: list[ModelLandingClassAction] = []
    deferred: list[ModelLandingClassAction] = []
    for short, step, head in wanted:
        if short in acted:
            continue
        action = ModelLandingClassAction(
            short=short,
            step=step,
            head=head,
            run_id=runs.get(short) if step == "rerun_run" else None,
        )
        cost = CLASS_COST[step]
        if cost > budget:
            deferred.append(action)
            continue
        budget -= cost
        plan.append(action)
        if step == "rerun_run":
            stale_mem[short] = stale_memory_next(
                stale_mem.get(short), head, f"{STALE_COPY_MEMO}{runs[short]}", now=now
            )
        elif step in ("rerun", "update_branch"):
            stale_mem[short] = stale_memory_next(
                stale_mem.get(short), head, step, now=now
            )
        else:
            retarget_mem[short] = retarget_memory_next(
                retarget_mem.get(short), head, step, now=now
            )

    def live(key: str) -> bool:
        return request.pr_states.get(key, "UNKNOWN").upper() not in TERMINAL_STATES

    summary = ModelLandingClassSummary(
        stale_rerun=_shorts(plan, "rerun"),
        stale_refresh=_shorts(plan, "update_branch"),
        stale_copy_rerun=_shorts(plan, "rerun_run"),
        retarget=_shorts(plan, "retarget"),
        retarget_ask=_shorts(plan, "comment"),
        retarget_wait=tuple(
            sorted(e.short for e in request.retarget if e.step == "wait")
        ),
        pending_gated={
            e.short: e.names
            for e in sorted(request.pending_gated, key=lambda e: e.short)
        },
        deferred=tuple(sorted(a.short for a in deferred)),
        cap=request.class_cap,
        reads=request.class_reads,
    )
    return ModelLandingClassPlanResult(
        plan=tuple(plan),
        deferred=tuple(deferred),
        stale_memory={k: v for k, v in sorted(stale_mem.items()) if live(k)},
        retarget_memory={k: v for k, v in sorted(retarget_mem.items()) if live(k)},
        summary=summary,
    )


def _shorts(plan: list[ModelLandingClassAction], step: ClassStep) -> tuple[str, ...]:
    return tuple(sorted(a.short for a in plan if a.step == step))


class HandlerPrLandingClassPlan:
    """The landing tick's class plan: pure definition-B compute over the tick's class candidates."""

    def handle(
        self, request: ModelLandingClassPlanRequest
    ) -> ModelLandingClassPlanResult:
        return plan_landing_classes(request)


__all__: list[str] = [
    "CLASS_COST",
    "CLASS_KIND",
    "HandlerPrLandingClassPlan",
    "plan_landing_classes",
    "retarget_memory_next",
    "stale_memory_next",
]
