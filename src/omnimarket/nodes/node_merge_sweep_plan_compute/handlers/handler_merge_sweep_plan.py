# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lanes one merge sweep dispatches (OMN-20676).

A port of the merge-sweep skill's ``plan`` rule and the sweep workflow's dispatch cap. The lanes
come out in order: diagnose, escalation, land-chain-head, fix. Load decides only placement
(``lab_only``); it never removes a lane. Merged or closed PRs, live-owned PRs and PRs of a
repository outside the scope are skipped with a reason. A chain head that is red is not landed:
it leads its repository's fix lane. The cap (``max_lanes``) defers the lanes beyond it to the
next sweep and never filters or reorders one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..models import (
    DEFAULT_MAX_LANES,
    MAX_LANES_CEILING,
    ModelMergeSweepLane,
    ModelMergeSweepLanePr,
    ModelMergeSweepOwner,
    ModelMergeSweepPlanRequest,
    ModelMergeSweepPlanResult,
    ModelMergeSweepReading,
    ModelMergeSweepSkipped,
)

# The repositories a sweep lane may work in. Anything else (a parked repository) is skipped.
FLEET_REPOS = frozenset(
    {
        "omnibase_core",
        "omnibase_spi",
        "omnibase_compat",
        "omnibase_infra",
        "omnibase_internal",
        "omniclaude",
        "omniclaude-internal",
        "omnimarket",
        "omnidash",
        "omniintelligence",
        "omninode_infra",
    }
)
LOAD_PER_CORE_BAR = 1.0
MAX_PRS_PER_LANE = 5

_NO_OWNER = ModelMergeSweepOwner(state="none")


def pr_key(token: str) -> str:
    """``OmniNode-ai/omnimarket#3059`` and ``omnimarket#3059`` are the same key."""
    return str(token).strip().strip("`").split("/")[-1].lower()


def repo_of(key: str) -> str:
    return key.split("#", 1)[0]


def max_lanes_of(raw: int | None) -> int:
    """The dispatch cap: absent or zero reads as the default, anything else is clamped to 1..12."""
    return max(1, min(MAX_LANES_CEILING, raw or DEFAULT_MAX_LANES))


def _lab_only(reading: ModelMergeSweepReading) -> bool:
    cpus = float(reading.cpus or 0)
    if reading.load1 is None or cpus <= 0:
        return True  # unread load: place on the lab, never withhold
    return float(reading.load1) / cpus > LOAD_PER_CORE_BAR


def _owner_gate(
    key: str,
    state: str,
    owner: ModelMergeSweepOwner | None,
    scope: frozenset[str],
    skipped: list[ModelMergeSweepSkipped],
) -> dict[str, Any] | None:
    if repo_of(key) not in scope:
        skipped.append(ModelMergeSweepSkipped(pr=key, why="out-of-scope-repo"))
        return None
    if str(state).upper() != "OPEN":
        skipped.append(ModelMergeSweepSkipped(pr=key, why="merged-or-closed"))
        return None
    held = owner or _NO_OWNER
    if held.state == "live":
        skipped.append(
            ModelMergeSweepSkipped(pr=key, why=f"live-owner lane={held.lane}")
        )
        return None
    if held.state not in {"none", "stale"}:
        skipped.append(
            ModelMergeSweepSkipped(pr=key, why=f"owner-unread state={held.state}")
        )
        return None
    entry: dict[str, Any] = {"pr": key}
    if held.state == "stale":
        entry["supersedes_claim"] = held.claim_ts
    return entry


def _head_first(heads: list[str]) -> Callable[[dict[str, Any]], tuple[bool, int]]:
    """Sort key: the repository's red chain heads first, in their order; the rest keep theirs."""

    def key(entry: dict[str, Any]) -> tuple[bool, int]:
        pr = str(entry["pr"])
        return (pr not in heads, heads.index(pr) if pr in heads else 0)

    return key


def _lane(
    kind: str, repo: str | None, prs: list[dict[str, Any]], **extra: Any
) -> ModelMergeSweepLane:
    return ModelMergeSweepLane(
        kind=kind,
        repo=repo,
        prs=[ModelMergeSweepLanePr(**p) for p in prs],
        **extra,
    )


class HandlerMergeSweepPlan:
    """Plan the lanes of one merge sweep from one evaluated reading."""

    def handle(self, request: ModelMergeSweepPlanRequest) -> ModelMergeSweepPlanResult:
        reading = request.reading
        scope = frozenset(reading.scope or FLEET_REPOS)
        skipped: list[ModelMergeSweepSkipped] = []
        lanes: list[ModelMergeSweepLane] = []
        taken: set[str] = set()

        if reading.controller is None:
            stalled, reasons = True, ["controller unread"]
        else:
            stalled, reasons = (
                reading.controller.stalled,
                reading.controller.reasons or ["stalled"],
            )
        why: list[str] = []
        if stalled:
            why += [f"controller: {r}" for r in reasons]
        under_floor = (
            reading.product.under_floor if reading.product is not None else ["unread"]
        )
        if under_floor:
            why.append("under floor: " + ",".join(under_floor))
        if why:
            lanes.append(_lane("diagnose", None, [], reasons=why))

        for e in reading.escalations or ():
            key = pr_key(e.pr)
            entry = _owner_gate(key, e.state, e.owner, scope, skipped)
            if entry and key not in taken:
                taken.add(key)
                lanes.append(_lane("escalation", repo_of(key), [entry]))

        red_heads: dict[str, list[str]] = {}
        for h in reading.chain_heads or ():
            key = pr_key(f"{h.repo}#{h.number}")
            if h.cls == "red":
                red_heads.setdefault(h.repo, []).append(key)
                continue
            if h.cls != "green-unarmed" or key in taken:
                continue
            entry = _owner_gate(key, h.state, h.owner, scope, skipped)
            if entry:
                taken.add(key)
                entry["children"] = list(h.children)
                lanes.append(_lane("land-chain-head", h.repo, [entry]))

        fix_by_repo: dict[str, list[dict[str, Any]]] = {}
        for r in reading.reds or ():
            key = pr_key(f"{r.repo}#{r.number}")
            if key in taken:
                continue
            entry = _owner_gate(key, r.state, r.owner, scope, skipped)
            if not entry:
                continue
            taken.add(key)
            entry.update({"ticket": r.ticket, "classes": list(r.classes or [])})
            fix_by_repo.setdefault(r.repo, []).append(entry)
        for repo in sorted(fix_by_repo):
            heads = red_heads.get(repo, [])
            prs = sorted(fix_by_repo[repo], key=_head_first(heads))
            for i in range(0, len(prs), MAX_PRS_PER_LANE):
                lanes.append(_lane("fix", repo, prs[i : i + MAX_PRS_PER_LANE]))

        cap = max_lanes_of(request.max_lanes)
        dispatch = lanes[:cap]
        load_per_core = (
            float(reading.load1) / float(reading.cpus)
            if reading.load1 is not None and reading.cpus
            else None
        )
        return ModelMergeSweepPlanResult(
            now=reading.now,
            lab_only=_lab_only(reading),
            load_per_core=load_per_core,
            lanes=lanes,
            skipped=skipped,
            dispatch=dispatch,
            deferred=len(lanes) - len(dispatch),
        )
