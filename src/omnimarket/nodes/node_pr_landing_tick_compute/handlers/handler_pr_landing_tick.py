# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing tick decision: the decision node with free worker slots shared across repositories.

The decision hands free worker slots out in its R4 priority order, so a dozen old reds in one
repository take every slot while another repository's reds wait behind them. This handler keeps
the decision and changes only which of its own dispatch candidates get this tick's slots:

1. a probe run with the worker cap lifted lists every PR the decision would dispatch now;
2. the slots the decision filled this tick go round-robin across repositories, the repository
   holding the fewest leases first, each repository's candidates in the decision's order;
3. the real run marks each candidate that did not get a slot with the decision's own ``hold``
   suspension, so the decision skips it this tick and changes nothing else. The token holder, an
   uncovered PR and a companion member are never deferred. The deferral lives only inside this
   call: the caller's facts and state never see it.

When the real run does not dispatch exactly the fair set the plain decision is returned, so
fairness can defer a dispatch but never lose one.
"""

from __future__ import annotations

import copy
from collections import Counter
from collections.abc import Callable
from typing import Any, Final

from omnimarket.models.landing_decision import (
    EnumLandingActionKind,
    EnumLandingSuspension,
    ModelLandingDecision,
    ModelLandingFacts,
)
from omnimarket.nodes.node_pr_landing_decision_compute import decide_landing

FAIR_DEFER: Final[str] = EnumLandingSuspension.HOLD.value

Decide = Callable[[dict[str, Any]], dict[str, Any]]


def repo_of(subject: str) -> str:
    """The repository of a subject: the text before ``#``, which is what the live controller shares by."""
    return subject.split("#", 1)[0]


def _dispatched(decision: dict[str, Any]) -> list[str]:
    return [
        str(a["subject"])
        for a in decision.get("actions", [])
        if a.get("kind") == EnumLandingActionKind.DISPATCH_WORKER.value
    ]


def _exempt(facts: dict[str, Any]) -> set[str]:
    state = facts.get("state") or {}
    out = {str(state["token_holder"])} if state.get("token_holder") else set()
    out |= {str(u.get("pr")) for u in state.get("uncovered") or ()}
    for comp in facts.get("companions") or ():
        out |= {str(m.get("pr")) for m in comp.get("members") or ()}
    return out


def fair_pick(
    candidates: list[str], leased: list[str], free: int, exempt: set[str]
) -> list[str]:
    """The candidates that get ``free`` slots: exempt ones first, then round-robin across repos."""
    count = Counter(repo_of(s) for s in leased)
    chosen = [c for c in candidates if c in exempt][:free]
    for c in chosen:
        count[repo_of(c)] += 1
    queues: dict[str, list[str]] = {}
    for c in candidates:
        if c not in chosen:
            queues.setdefault(repo_of(c), []).append(c)
    order = {c: i for i, c in enumerate(candidates)}
    while len(chosen) < free and any(queues.values()):
        repo = min(
            (r for r, q in queues.items() if q),
            key=lambda r: (count[r], order[queues[r][0]]),
        )
        chosen.append(queues[repo].pop(0))
        count[repo] += 1
    return chosen


def fair_decide(facts: dict[str, Any], decide: Decide) -> dict[str, Any]:
    """The decision of ``decide`` with this tick's free slots shared across repositories."""
    plain = decide(facts)
    # The slots this tick really has are the ones the decision filled: it counts the leases a
    # result released this tick, which the facts' lease list still carries.
    dispatched = _dispatched(plain)
    free = len(dispatched)
    if free == 0:
        return plain
    probe = copy.deepcopy(facts)
    probe["policy"]["max_workers"] = (
        len((facts.get("state") or {}).get("leases") or ())
        + len(facts.get("prs") or ())
        + len(facts.get("companions") or ())
        + 1
    )
    candidates = _dispatched(decide(probe))
    if len(candidates) <= free:
        return plain
    leased = [
        str(le.get("pr"))
        for le in (plain.get("next_state") or {}).get("leases") or ()
        if str(le.get("pr")) not in dispatched
    ]
    prs = {str(p.get("pr")) for p in facts.get("prs") or ()}
    exempt = _exempt(facts) | {c for c in candidates if c not in prs}
    chosen = fair_pick(candidates, leased, free, exempt)
    if sorted(chosen) == sorted(dispatched):
        return plain
    deferred = {c for c in candidates if c not in chosen and c not in exempt}
    fair = copy.deepcopy(facts)
    for pr in fair.get("prs") or ():
        if pr.get("pr") in deferred:
            pr["suspensions"] = [*(pr.get("suspensions") or ()), FAIR_DEFER]
    decision = decide(fair)
    if sorted(_dispatched(decision)) != sorted(chosen):
        return plain
    return decision


def _run(doc: dict[str, Any]) -> dict[str, Any]:
    decision = decide_landing(ModelLandingFacts.model_validate(doc))
    return decision.model_dump(mode="json")


def decide_landing_fair_share(facts: ModelLandingFacts) -> ModelLandingDecision:
    """One tick with the free worker slots shared across repositories."""
    doc = fair_decide(facts.model_dump(mode="json"), _run)
    return ModelLandingDecision.model_validate(doc)


class HandlerPrLandingTick:
    """The landing tick decision: pure definition-B compute with per-repository fair share."""

    def handle(self, request: ModelLandingFacts) -> ModelLandingDecision:
        return decide_landing_fair_share(request)


__all__: list[str] = [
    "FAIR_DEFER",
    "HandlerPrLandingTick",
    "decide_landing_fair_share",
    "fair_decide",
    "fair_pick",
]
