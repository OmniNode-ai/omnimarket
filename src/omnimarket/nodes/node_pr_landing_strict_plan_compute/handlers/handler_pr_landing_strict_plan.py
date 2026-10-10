# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The order of a landing tick's merges and branch updates on the bases that require branches up to date.

``HandlerPrLandingStrictPlan.handle(ModelLandingStrictPlanRequest) -> ModelLandingStrictPlanResult``
(definition-B). On a base whose branch protection requires the branch up to date, every merge makes every
other head behind, so a second updated head is behind again by the next tick. For each such base this
plans, from the decision's merge and update-branch actions:

* which bases need ordering: the tick acts on them more than once, or a declared cause fix or a yielded PR
  acts there, and the base is strict or unread (an unread base fails closed);
* the rank of each action there: a declared cause fix first, then the M4 delegation PRs, then the rest in
  the decision's order, a PR that yielded the update slot last;
* a declared fix that acts this tick, or whose head has had checks pending under an hour, holds its base:
  every other PR's merge and update there is held, and a held update waits for a later tick;
* a strict base takes one update a tick, less the merges the tick performs there; the surplus updates wait;
* a PR whose checks failed on two fresh heads in a row, each produced by our update, yields the update slot
  until its head changes (the strict-slot memory, carried in the request and returned as ``slot``);
* the M4 delegation PRs' actions of each kind move ahead of the others of that kind, except where a strict
  base ranked them already.

The read of a base's setting is an effect and stays with the caller: ``bases_to_read`` names the bases the
plan looked up, and one the caller has not read is ordered like a strict one.
"""

from __future__ import annotations

import contextlib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Final

from omnimarket.nodes.node_pr_landing_strict_plan_compute.models.model_landing_strict_plan import (
    ModelLandingStrictPlanRequest,
    ModelLandingStrictPlanResult,
    ModelStrictAction,
    ModelStrictLivePr,
    ModelStrictPending,
    ModelStrictSlotEntry,
)

# One update-branch per strict base per tick: every merge there re-stales every other head.
STRICT_UPDATE_CAP: Final[int] = 1
# Consecutive failures on heads our strict-base updates produced yield the update slot.
STRICT_YIELD_STRIKES: Final[int] = 2
# A declared fix whose head has been pending less than this holds its base.
FIX_HOLD_PENDING_S: Final[int] = 3600
STRICT_SLOT_KEEP_S: Final[int] = 7 * 86400
# The kinds whose M4 actions move ahead of the others of the same kind.
M4_ORDERED_KINDS: Final[tuple[str, ...]] = (
    "merge",
    "update_branch",
    "dispatch_worker",
    "rerun",
    "eligibility_rerun",
)
STAMP_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%SZ"
# A fresh head named by an abbreviated sha is resolved against the live head.
FRESH_KEEP: Final[int] = 5


def short_key(subject: str) -> str:
    """The node's ``owner/repo#n`` to the watcher's ``repo#n``."""
    return subject.split("/", 1)[-1]


def strict_update_slots(merges: int, cap: int = STRICT_UPDATE_CAP) -> int:
    """The node's own update_branch actions one strict base takes this tick."""
    return max(0, cap - min(cap, max(0, merges - 1)))


def strict_merge_plan(
    merges: Sequence[tuple[int, str]],
    strict_of: Mapping[str, bool | None],
    extra: Iterable[str] = (),
) -> dict[str, list[int]]:
    """Group the merge and update actions by ``repo:base`` and keep the bases that need ordering."""
    by_base: dict[str, list[int]] = {}
    for idx, key in merges:
        by_base.setdefault(key, []).append(idx)
    extra = set(extra)
    return {
        k: v
        for k, v in by_base.items()
        if (len(v) > 1 or k in extra) and strict_of.get(k) is not False
    }


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _has_state(entry: ModelStrictSlotEntry) -> bool:
    return bool(
        entry.from_head
        or entry.fresh
        or entry.strikes
        or entry.yielded
        or entry.pending
    )


def strict_slot_observe(
    mem: Mapping[str, ModelStrictSlotEntry],
    live: Mapping[str, ModelStrictLivePr],
    now_ts: str,
    track: Iterable[str] = (),
) -> dict[str, ModelStrictSlotEntry]:
    """Observe fresh CI, foreign pushes and bounded pending fixes without changing the memory."""
    tracked = set(track)
    out: dict[str, ModelStrictSlotEntry] = {}
    now = _parse(now_ts)
    for short in sorted(set(mem) | tracked):
        row = mem.get(short) or ModelStrictSlotEntry()
        lv = live.get(short)
        if lv is None:
            try:
                at = _parse(str(row.at))
            except ValueError:
                continue
            if (now - at).total_seconds() < STRICT_SLOT_KEEP_S and _has_state(row):
                out[short] = row
            continue
        head, rollup = lv.head, lv.rollup.upper()
        fresh = [head if head.startswith(h) else h for h in row.fresh]
        strikes = [head if head.startswith(h) else h for h in row.strikes]
        from_head, yielded, pending = row.from_head, row.yielded, row.pending
        if from_head and head != from_head:
            if head not in fresh:
                fresh.append(head)
            from_head = None
        if yielded:
            if head != yielded or rollup == "SUCCESS":
                strikes, fresh, yielded = [], [], None
        elif head not in fresh and head != from_head and strikes:
            strikes, fresh = [], []
        if head in fresh:
            if rollup in ("FAILURE", "ERROR") and head not in strikes:
                strikes.append(head)
            elif rollup == "SUCCESS":
                strikes = []
        if len(strikes) >= STRICT_YIELD_STRIKES and not yielded:
            yielded = head
        if short in tracked and rollup == "PENDING":
            if not pending or pending.head != head:
                pending = ModelStrictPending(head=head, since=now_ts)
        else:
            pending = None
        entry = ModelStrictSlotEntry(
            from_head=from_head,
            fresh=tuple(fresh[-FRESH_KEEP:]),
            strikes=tuple(strikes),
            yielded=yielded,
            pending=pending,
            at=now_ts,
        )
        if _has_state(entry):
            out[short] = entry
    return out


def m4_order(
    acts: Sequence[ModelStrictAction],
    order: list[int],
    *,
    pinned: set[int],
    m4: Iterable[str],
) -> list[int]:
    """``order`` with the M4 delegation PRs' actions of each kind moved ahead of the others of that kind."""
    wanted = frozenset(m4)
    out = list(order)
    if not wanted:
        return out

    def rank(i: int) -> int:
        subject = acts[i].subject
        return 0 if "#" in subject and short_key(subject) in wanted else 1

    for kind in M4_ORDERED_KINDS:
        places = [
            p for p, i in enumerate(out) if i not in pinned and acts[i].kind == kind
        ]
        for p, i in zip(
            places, sorted((out[p] for p in places), key=rank), strict=True
        ):
            out[p] = i
    return out


def _base_key(live: Mapping[str, ModelStrictLivePr], short: str) -> str:
    lv = live.get(short)
    base = lv.base if lv is not None else ""
    return f"{short.partition('#')[0]}:{base}" if base else ""


def _rank(
    short: str,
    fixes: Mapping[str, str],
    m4: frozenset[str],
    yielded: Mapping[str, object],
) -> int:
    """A declared fix first, then an M4 PR, then the rest; a PR that yielded the update slot last."""
    if short in yielded:
        return 3
    if short in fixes:
        return 0
    return 1 if short in m4 else 2


def plan_strict_order(
    request: ModelLandingStrictPlanRequest,
) -> ModelLandingStrictPlanResult:
    """Order the tick's merges and updates on its strict bases, and defer the surplus updates."""
    acts = request.actions
    live = request.live
    now = request.now.astimezone(UTC)
    now_ts = now.replace(microsecond=0).strftime(STAMP_FORMAT)
    m4 = frozenset(request.m4)
    fixes = {
        s: request.fix_why.get(s, "cause lease") for s in sorted(set(request.fix_prs))
    }
    for claim in request.cause_fix_claims:
        fixes.setdefault(claim.pr, f"cause CLAIM lane={claim.lane} cause={claim.cause}")
    slot = strict_slot_observe(request.slot_memory, live, now_ts, track=fixes)
    yielded = {s: e for s, e in slot.items() if e.yielded}
    action_prs = {
        short_key(a.subject) for a in acts if a.kind in ("merge", "update_branch")
    }
    holding: dict[str, list[str]] = {}
    for short in fixes:
        lv = live.get(short)
        if lv is None or lv.state.upper() != "OPEN" or short in yielded:
            continue
        if lv.merge_state == "DIRTY" or lv.mergeable == "CONFLICTING":
            continue
        pending = (slot.get(short) or ModelStrictSlotEntry()).pending
        young = False
        if pending:
            with contextlib.suppress(ValueError):
                young = (
                    now - _parse(pending.since)
                ).total_seconds() < FIX_HOLD_PENDING_S
        if short in action_prs or young:
            key = _base_key(live, short)
            if key:
                holding.setdefault(key, []).append(short)
    extra = set(holding) | {_base_key(live, s) for s in yielded if s in action_prs}
    extra.discard("")

    merges: list[tuple[int, str]] = []
    for i, a in enumerate(acts):
        if a.kind in ("merge", "update_branch") and "#" in a.subject:
            key = _base_key(live, short_key(a.subject))
            if key:
                merges.append((i, key))
    counts = Counter(k for _, k in merges)
    wanted = sorted(k for k, n in counts.items() if n > 1 or k in extra)
    ordered_idx = (
        strict_merge_plan(merges, {k: request.strict_of.get(k) for k in wanted}, extra)
        if wanted
        else {}
    )

    base_of = {i: key for key, idxs in ordered_idx.items() for i in idxs}
    deferred: dict[int, str] = {}
    details: dict[int, str] = {}
    held: dict[int, str] = {}
    order = list(range(len(acts)))
    ordered: dict[str, tuple[int, ...]] = {}
    for key, idxs in ordered_idx.items():
        shorts = {i: short_key(acts[i].subject) for i in idxs}
        ranked = sorted(idxs, key=lambda i: _rank(shorts[i], fixes, m4, yielded))
        for pos, idx in zip(idxs, ranked, strict=True):
            order[pos] = idx
        ordered[key] = tuple(ranked)
        for i in ranked:
            short = shorts[i]
            if short not in fixes and holding.get(key):
                fix = holding[key][0]
                held[i] = key
                details[i] = (
                    f"strict base {key}: cause fix {fix} goes first ({fixes[fix]})"
                )
            if short in yielded and acts[i].kind == "update_branch":
                strikes = yielded[short].strikes
                deferred[i] = key
                details[i] = (
                    f"strict base {key}: {short} yields the update slot: ci failed or was cancelled "
                    f"on its fresh heads {', '.join(h[:7] for h in strikes)}; "
                    "it takes the slot again when its head changes"
                )
            elif i in held and acts[i].kind == "update_branch":
                deferred[i] = key
        merge_count = sum(
            1
            for i in ranked
            if acts[i].kind == "merge" and i not in held and shorts[i] not in yielded
        )
        updates = [
            i for i in ranked if acts[i].kind == "update_branch" and i not in deferred
        ]
        for i in updates[strict_update_slots(merge_count) :]:
            deferred[i] = key
    order = m4_order(acts, order, pinned=set(base_of), m4=m4)
    return ModelLandingStrictPlanResult(
        base_of=base_of,
        deferred_updates=deferred,
        details=details,
        held=held,
        holding={k: tuple(v) for k, v in holding.items()},
        fixes=fixes,
        order=tuple(order),
        ordered=ordered,
        slot=slot,
        yielded=yielded,
        bases_to_read=tuple(wanted),
    )


class HandlerPrLandingStrictPlan:
    """The landing tick's strict-base ordering: pure definition-B compute over the tick's actions."""

    def handle(
        self, request: ModelLandingStrictPlanRequest
    ) -> ModelLandingStrictPlanResult:
        return plan_strict_order(request)


__all__: list[str] = [
    "HandlerPrLandingStrictPlan",
    "m4_order",
    "plan_strict_order",
    "strict_merge_plan",
    "strict_slot_observe",
    "strict_update_slots",
]
