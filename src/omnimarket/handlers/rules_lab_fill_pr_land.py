# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The idle-slot pr-land fallback rule (OMN-20864): pure, shared by lab-fill selection and the tick.

Operator ruling 2026-10-10T04:08:23Z: idle lab capacity is never left idle while PRs are open; the
default work is a per-PR landing lane on an open PR the landing controller parked, escalated or left
red with no owner. Lab-fill selection plans those lanes for the slots its ordinary work leaves idle,
and the merge-throughput tick's lab-headroom FIX names them; both call ``plan_pr_land_fallback`` so the
two cannot disagree.

Gates, first match per PR:
1. An in-force HOLD row names the PR (``pr=``) or, with no ``pr=`` and no ``surface=``, its repository
   (``repo=``): SKIP_HELD with the hold id. The rows are read here, in code, from the hold source the
   request carries; an unread source is a typed failure that plans nothing.
2. The PR is red now on a cause no lane can fix on it: the triage class says the base head carries
   every red (``dev_head``) or the red is a shared cause across PRs (``shared_cause``); or every red
   check it carries, aggregates aside, is red on at least ``shared_red_min_prs`` open PRs of its
   repository (the bus triage clusters armed peers only, so it calls an unarmed member its own red);
   or the caller observed such a cause: SKIP_BLOCKED_UNFIXABLE with the cause.
3. Its last landing lane was blocked on such a cause at this head and the cause is not observed
   cleared: SKIP_BLOCKED_UNFIXABLE. A new head, or the cause cleared (base-red: green, or a red the
   triage calls the PR's own; any other cause: the caller's ``cleared_causes``), makes it eligible.
4. A live lane owns it, or it is a draft: OWNED.
5. It is neither escalated, parked nor red: HANDED_OFF (the landing controller lands it).
6. Its last landing lane was blocked at this head, on no typed cause, within the cooldown:
   UNCHANGED_INPUT.
Eligible PRs rank escalated, parked, unowned-red, then by PR; past the idle slots they are
DISPATCH_LIMIT. Nothing here reads a file, the network or the clock.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime, timedelta

from omnimarket.events.pr_landing.enum_pr_landing_state import EnumPrLandingState
from omnimarket.models.ci_red_triage import EnumCiRedClass
from omnimarket.models.lab_fill import (
    EnumLabFillPrLandCause,
    EnumLabFillPrLandClass,
    EnumLabFillPrLandFailure,
    EnumLabFillSkipReason,
    ModelLabFillHoldSource,
    ModelLabFillOpenPr,
    ModelLabFillPrLandDecision,
    ModelLabFillPrLandDispatch,
    ModelLabFillPrLandFacts,
    ModelLabFillPrLandPlan,
)

# Triage classes whose red is not the PR's own: the base head carries every red, or the same check is
# red across the repository's PRs. A per-PR lane cannot fix either on the PR.
BASE_RED_CLASSES = frozenset({EnumCiRedClass.DEV_HEAD, EnumCiRedClass.SHARED_CAUSE})
_ORDER = tuple(EnumLabFillPrLandClass)
# Checks that never form a shared cause: the CI Summary aggregate and the hostile-review family, the
# patterns node_pr_lifecycle_triage_compute's red rules set aside (SUMMARY_CHECK_RE, REVIEWER_POOL_RE).
AGGREGATE_CHECK_RE = re.compile(r"^ci summary$|hostile review", re.I)
_PR_ID_RE = re.compile(r"[A-Za-z0-9_.-]+#[0-9]+")


def _stamp(value: str) -> datetime | None:
    try:
        at = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return at.astimezone(UTC) if at.tzinfo is not None else None


def pr_key(value: str) -> str:
    """``owner/repo#n`` or ``repo#n`` as the bare, lower-case ``repo#n``."""
    return value.strip().rsplit("/", 1)[-1].lower()


def _repo(pr: str) -> str:
    return pr.partition("#")[0]


def in_force_holds(lines: tuple[str, ...], now: datetime) -> dict[str, str]:
    """Hold scope (``repo#n`` or ``repo``) -> the id of the earliest in-force HOLD naming it.

    A HOLD row is in force from its stamp until a RELEASE row names its id (``re=``) or its
    ``until=`` passes. It scopes the PRs its ``pr=`` cell lists; with no ``pr=`` cell and no
    ``surface=`` cell (a lab lease), the repositories its ``repo=`` cell lists. Free text never scopes.
    """
    holds: list[tuple[str, frozenset[str]]] = []
    released: set[str] = set()
    for line in lines:
        cells = [cell.strip() for cell in line.split("|")]
        at = _stamp(cells[0]) if cells else None
        if len(cells) < 2 or at is None or at > now:
            continue
        values = dict(cell.split("=", 1) for cell in cells[2:] if "=" in cell)
        if cells[1] == "RELEASE":
            released.update(r.strip() for r in values.get("re", "").split(","))
            continue
        hold_id = values.get("id", "").strip()
        if cells[1] != "HOLD" or not hold_id:
            continue
        until = _stamp(values["until"]) if "until" in values else None
        if until is not None and until <= now:
            continue
        prs = {pr_key(p) for p in _PR_ID_RE.findall(values.get("pr", ""))}
        if not prs and "surface" not in values:
            prs = {
                r.strip().lower()
                for r in values.get("repo", "").split(",")
                if r.strip()
            }
        if prs:
            holds.append((hold_id, frozenset(prs)))
    scoped: dict[str, str] = {}
    for hold_id, scopes in holds:
        if hold_id in released:
            continue
        for scope in scopes:
            scoped.setdefault(scope, hold_id)
    return scoped


def pr_class(pr: ModelLabFillOpenPr) -> EnumLabFillPrLandClass | None:
    """Escalated, parked or unowned-red (owner is gated before), else None."""
    if pr.controller_escalated or pr.landing_state is EnumPrLandingState.NEEDS_AGENT:
        return EnumLabFillPrLandClass.ESCALATED
    if pr.controller_parked or pr.landing_state is EnumPrLandingState.PARKED:
        return EnumLabFillPrLandClass.PARKED
    if pr.ci_verdict == "RED":
        return EnumLabFillPrLandClass.UNOWNED_RED
    return None


def _base_red_now(pr: ModelLabFillOpenPr) -> bool:
    return pr.ci_verdict == "RED" and pr.red_class in BASE_RED_CLASSES


def _base_red_cleared(pr: ModelLabFillOpenPr) -> bool:
    """Positive evidence only: green, or red with a triage class that is the PR's own."""
    return pr.ci_verdict == "GREEN" or (
        pr.ci_verdict == "RED"
        and pr.red_class is not None
        and pr.red_class not in BASE_RED_CLASSES
    )


def shared_reds(
    prs: tuple[ModelLabFillOpenPr, ...], min_prs: int
) -> dict[str, tuple[int, tuple[str, ...]]]:
    """``repo#n`` -> (smallest member count, its reds) for each red PR whose every red check, aggregates
    aside, is red on at least ``min_prs`` open PRs of its repository."""
    reds = {
        pr_key(pr.pr): tuple(
            c for c in pr.red_contexts if not AGGREGATE_CHECK_RE.search(c)
        )
        for pr in prs
        if pr.ci_verdict == "RED"
    }
    members = Counter(
        (_repo(key), check) for key, checks in reds.items() for check in set(checks)
    )
    shared: dict[str, tuple[int, tuple[str, ...]]] = {}
    for key, checks in reds.items():
        counts = [members[(_repo(key), check)] for check in checks]
        if counts and min(counts) >= min_prs:
            shared[key] = (min(counts), checks)
    return shared


def _decide(
    pr: ModelLabFillOpenPr,
    holds: dict[str, str],
    shared: dict[str, tuple[int, tuple[str, ...]]],
    now: datetime,
    cooldown: timedelta,
) -> ModelLabFillPrLandDecision:
    key = pr_key(pr.pr)

    def decision(
        reason: EnumLabFillSkipReason | None,
        detail: str = "",
        **extra: object,
    ) -> ModelLabFillPrLandDecision:
        return ModelLabFillPrLandDecision.model_validate(
            {
                "pr": key,
                "head_sha": pr.head_sha,
                "reason": reason,
                "pr_class": pr_class(pr),
                "detail": detail,
                **extra,
            }
        )

    hold_id = holds.get(key) or holds.get(_repo(key), "")
    if hold_id:
        return decision(EnumLabFillSkipReason.SKIP_HELD, hold_id, hold_id=hold_id)
    if _base_red_now(pr):
        reds = ",".join(pr.red_contexts)
        return decision(
            EnumLabFillSkipReason.SKIP_BLOCKED_UNFIXABLE,
            f"base-red:{pr.red_class}:{reds}",
            cause=EnumLabFillPrLandCause.BASE_RED,
        )
    if key in shared:
        members, checks = shared[key]
        return decision(
            EnumLabFillSkipReason.SKIP_BLOCKED_UNFIXABLE,
            f"base-red:shared:{members} open PRs:{','.join(checks)}",
            cause=EnumLabFillPrLandCause.BASE_RED,
        )
    if pr.causes:
        cause = sorted(pr.causes, key=list(EnumLabFillPrLandCause).index)[0]
        return decision(
            EnumLabFillSkipReason.SKIP_BLOCKED_UNFIXABLE,
            f"{cause}:observed",
            cause=cause,
        )
    last = pr.last_outcome
    same_head = last is not None and last.head_sha in ("", pr.head_sha)
    if (
        last is not None
        and same_head
        and last.outcome.lower() == "blocked"
        and last.cause is not None
    ):
        # Base-red is read here and clears on positive evidence (green, or a red the triage calls the
        # PR's own); every other cause, a hold kept outside the hold source included, clears only when
        # the caller observed it cleared.
        cleared = last.cause in pr.cleared_causes or (
            last.cause is EnumLabFillPrLandCause.BASE_RED and _base_red_cleared(pr)
        )
        if not cleared:
            return decision(
                EnumLabFillSkipReason.SKIP_BLOCKED_UNFIXABLE,
                f"{last.cause}:last-outcome:{last.lane or 'blocked'}@{pr.head_sha[:12]}",
                cause=last.cause,
            )
    if pr.owner:
        return decision(EnumLabFillSkipReason.OWNED, pr.owner)
    if pr.draft:
        return decision(EnumLabFillSkipReason.OWNED, "draft")
    if pr_class(pr) is None:
        return decision(EnumLabFillSkipReason.HANDED_OFF, "landing-controller")
    if (
        last is not None
        and same_head
        and last.outcome.lower() == "blocked"
        and last.cause is None
    ):
        at = _stamp(last.at)
        if at is not None and now - at < cooldown:
            return decision(
                EnumLabFillSkipReason.UNCHANGED_INPUT,
                f"blocked:{last.lane or 'lane'}@{pr.head_sha[:12]}",
            )
    return decision(None)


def plan_pr_land_fallback(
    facts: ModelLabFillPrLandFacts, idle_slots: int, now: str
) -> ModelLabFillPrLandPlan:
    """Plan pr-land lanes for ``idle_slots`` slots from the PR-land facts (pure)."""
    at = _stamp(now)
    if at is None:
        raise ValueError("now must be an ISO timestamp with a timezone")
    slots = max(0, idle_slots)
    source: ModelLabFillHoldSource = facts.holds
    if not source.read:
        return ModelLabFillPrLandPlan(
            idle_slots=slots,
            reason=f"hold-source-unreadable:{source.source}:{source.error or 'unread'}",
            failure=EnumLabFillPrLandFailure.HOLD_SOURCE_UNREADABLE,
        )
    holds = in_force_holds(source.lines, at)
    cooldown = timedelta(hours=facts.cooldown_hours)
    by_pr = {pr_key(pr.pr): pr for pr in facts.prs}
    shared = shared_reds(tuple(by_pr.values()), facts.shared_red_min_prs)
    decided = [_decide(pr, holds, shared, at, cooldown) for pr in by_pr.values()]
    ranked = sorted(
        (
            (
                _ORDER.index(d.pr_class),
                _repo(d.pr),
                int(d.pr.partition("#")[2]),
                d.pr_class,
                d,
            )
            for d in decided
            if d.reason is None and d.pr_class is not None
        ),
        key=lambda item: item[:3],
    )
    chosen = {d.pr for *_, d in ranked[:slots]}
    decisions = tuple(
        d
        if d.reason is not None or d.pr in chosen
        else d.model_copy(
            update={
                "reason": EnumLabFillSkipReason.DISPATCH_LIMIT,
                "detail": f"no-idle-slot:{slots}",
            }
        )
        for d in decided
    )
    dispatch = tuple(
        ModelLabFillPrLandDispatch(
            pr=d.pr,
            repo=_repo(d.pr),
            head_sha=d.head_sha,
            head_ref=by_pr[d.pr].head_ref,
            ticket=by_pr[d.pr].ticket,
            title=by_pr[d.pr].title,
            pr_class=cls,
        )
        for *_, cls, d in ranked[:slots]
    )
    reason = ""
    if not ranked:
        counts = Counter(str(d.reason) for d in decisions)
        read = f"{len(decisions)} open PRs read"
        if counts:
            read += ": " + ", ".join(f"{k}={counts[k]}" for k in sorted(counts))
        reason = f"no parked, escalated or unowned-red open PR for {slots} idle slots ({read})"
    elif not slots:
        reason = (
            f"no idle slot for {len(ranked)} parked, escalated or unowned-red open PRs"
        )
    return ModelLabFillPrLandPlan(
        idle_slots=slots, decisions=decisions, dispatch=dispatch, reason=reason
    )


__all__: list[str] = [
    "AGGREGATE_CHECK_RE",
    "BASE_RED_CLASSES",
    "in_force_holds",
    "plan_pr_land_fallback",
    "pr_class",
    "pr_key",
    "shared_reds",
]
