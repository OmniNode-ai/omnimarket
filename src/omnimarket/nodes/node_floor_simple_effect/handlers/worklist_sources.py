# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The sources of the board's work list (OMN-23184).

Reads three things and no more: the pull request watcher's state (open, non-draft, in-scope pull requests that are red or
conflicting, and the required checks red across several of them), the landing controller's state file (its landing leases
and the cause leases that hold shared reds), and the work-state projection (the tickets a lane left stopped, and the
pull requests the goal's ticket rule keeps in scope). An input that cannot be read makes the whole list unread (``None``),
never an empty one: a list read from half its sources would call unowned work owned or absent.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from omnimarket.models.floor_17a import ModelGoalScope
from omnimarket.models.floor_17a.model_worklist import (
    ModelWorklistItemIn,
    ModelWorklistOverlay,
)
from omnimarket.models.work_state_projection import (
    EnumScopeStatus,
    EnumWorkItemKind,
    EnumWorkItemState,
    ModelWorkStateResult,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    parse_time,
    tickets_of,
)


def short_key(ref: str) -> str:
    """``owner/repo#n`` as ``repo#n``; a cause key (no ``#``) unchanged."""
    return ref.rpartition("/")[2] if "#" in ref else ref


def projected_in_scope_prs(projection: ModelWorkStateResult | None) -> frozenset[str]:
    """The pull requests (``repo#n``) the work-state projection holds in scope.

    The projection judges a pull request through its ticket's chain to the goal's roots and its project, which the goal
    scope's lists alone cannot: a pull request named on no list but whose ticket sits under a root is in scope (the landing
    controller's ticket rule). A pull request on the paused list is paused in the projection as well.
    """
    if projection is None:
        return frozenset()
    return frozenset(
        item.key.removeprefix("pr:")
        for item in projection.items
        if item.kind is EnumWorkItemKind.PULL_REQUEST
        and item.scope is EnumScopeStatus.IN_SCOPE
    )


def pr_scope(
    scope: ModelGoalScope,
    projected: frozenset[str],
    key: str,
    facts: Mapping[str, Any],
) -> str:
    """The goal's verdict on one open pull request: the scope's lists first, then the projection's ticket rule.

    ``unlisted`` is the only verdict the projection lifts; a paused pull request stays paused whatever ticket it names.
    """
    where = scope.classify(
        key,
        str(facts.get("title", "")),
        str(facts.get("head_ref", "")),
        str(facts.get("body", "")),
    )
    if where == "unlisted" and key in projected:
        return "in_scope"
    return where


class LaneOwners:
    """Who the landing controller says holds a pull request: a landing lease on it, or the lease of a cause that holds it."""

    def __init__(
        self, landing: dict[str, str], cause: dict[str, str], parked: dict[str, str]
    ):
        self.landing = landing
        self.cause = cause
        self.parked = parked

    def of(self, pr: str) -> tuple[str, str]:
        """(landing lease, cause lease) for ``repo#n``; a parked cause is named as the cause owner."""
        return self.landing.get(pr, ""), self.cause.get(pr) or self.parked.get(pr, "")


def read_controller_owners(path: Path) -> LaneOwners | None:
    """The controller's state file (``leases`` and ``causes``), or ``None`` when it is absent or not a state."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        leases = state.get("leases") or []
        causes = state.get("causes") or []
        if not isinstance(leases, list) or not isinstance(causes, list):
            return None
        by_subject = {short_key(str(le["pr"])): int(le["lease_id"]) for le in leases}
        cause_keys = {str(c["key"]) for c in causes}
        landing: dict[str, str] = {}
        cause: dict[str, str] = {}
        parked: dict[str, str] = {}
        for subject, lease_id in by_subject.items():
            if subject not in cause_keys:
                landing[subject] = f"landing-L{lease_id}"
        for rec in causes:
            key = str(rec["key"])
            members = [short_key(str(m["pr"])) for m in rec.get("members") or ()]
            if key in by_subject:
                for member in members:
                    cause[member] = f"cause-L{by_subject[key]} {key}"
            elif rec.get("parked_until"):
                for member in members:
                    parked[member] = f"parked {key} until {rec['parked_until']}"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return LaneOwners(landing, cause, parked)


def _age(now: datetime, stamp: Any) -> int | None:
    try:
        return max(int((now - parse_time(stamp)).total_seconds()), 0)
    except (ValueError, TypeError):
        return None


def _red_since(value: Mapping[str, Any]) -> datetime | None:
    """When the oldest red required check of a pull request finished, from the watcher's ``red_facts`` rows."""
    stamps: list[datetime] = []
    for row in (value.get("ci") or {}).get("red_facts") or ():
        try:
            stamps.append(parse_time(row[3]))
        except (ValueError, TypeError, IndexError):
            continue
    return min(stamps) if stamps else None


def _open_scoped(
    prs: Mapping[str, Any],
    scope: ModelGoalScope,
    projected: frozenset[str],
    excluded_classes: Iterable[str],
) -> list[tuple[str, Mapping[str, Any]]]:
    """Open, non-draft, in-scope pull requests whose watcher class is not an excluded one, in key order."""
    out: list[tuple[str, Mapping[str, Any]]] = []
    for key in sorted(prs):
        value = prs[key]
        facts = value.get("facts", {})
        if facts.get("state") != "OPEN" or facts.get("draft", False):
            continue
        if str(value.get("cls", "")) in excluded_classes:
            continue
        if pr_scope(scope, projected, str(key), facts) != "in_scope":
            continue
        out.append((str(key), value))
    return out


def pr_items(
    open_scoped: list[tuple[str, Mapping[str, Any]]],
    owners: LaneOwners,
    now: datetime,
    conflict_states: tuple[str, ...],
) -> tuple[ModelWorklistItemIn, ...]:
    """Every open in-scope pull request that is red or conflicting, with its red checks, its age and its lease owners."""
    out: list[ModelWorklistItemIn] = []
    for key, value in open_scoped:
        facts = value.get("facts", {})
        red = tuple(str(n) for n in (value.get("ci") or {}).get("red") or ())
        merge_state = str((value.get("review") or {}).get("merge_state") or "")
        conflicting = merge_state in conflict_states
        if str(value.get("cls", "")) != "red" and not conflicting:
            continue
        landing, cause = owners.of(key)
        ticket = next(
            iter(
                tickets_of(str(facts.get("title", "")), str(facts.get("head_ref", "")))
            ),
            "",
        )
        what = ", ".join(red) if red else "no red check"
        out.append(
            ModelWorklistItemIn(
                kind="pr",
                key=key,
                title=str(facts.get("title", "")),
                detail=f"{'conflicting; ' if conflicting else ''}red: {what}",
                red_checks=red,
                prs=(key,),
                ticket=ticket,
                age_s=_age(now, facts.get("created_at")),
                landing_lease=landing,
                cause_lease=cause,
                conflicting=conflicting,
            )
        )
    return tuple(out)


def shared_red_items(
    open_scoped: list[tuple[str, Mapping[str, Any]]],
    owners: LaneOwners,
    now: datetime,
    after_s: int,
) -> tuple[ModelWorklistItemIn, ...]:
    """A required check red on two or more open in-scope pull requests of one repository, red for ``after_s`` or more.

    Owned when the controller holds a cause lease (or parks the cause) over any of its pull requests. The age is the oldest
    red result among the members.
    """
    groups: dict[tuple[str, str], list[tuple[str, Mapping[str, Any]]]] = {}
    for key, value in open_scoped:
        repo = key.partition("#")[0]
        for name in (value.get("ci") or {}).get("red") or ():
            groups.setdefault((repo, str(name)), []).append((key, value))
    out: list[ModelWorklistItemIn] = []
    for (repo, check), members in sorted(groups.items()):
        if len(members) < 2:
            continue
        since = [s for s in (_red_since(v) for _, v in members) if s is not None]
        age = max(int((now - min(since)).total_seconds()), 0) if since else None
        if age is None or age < after_s:
            continue
        keys = tuple(k for k, _ in members)
        cause = next((c for c in (owners.of(k)[1] for k in keys) if c), "")
        out.append(
            ModelWorklistItemIn(
                kind="shared_red",
                key=f"{repo}:{check}",
                title=f"{check} is red on {len(keys)} open pull requests of {repo}",
                detail=f"red on {', '.join(keys[:6])}"
                + (f" and {len(keys) - 6} more" if len(keys) > 6 else ""),
                red_checks=(check,),
                prs=keys,
                age_s=age,
                cause_lease=cause,
            )
        )
    return tuple(out)


def stopped_items(
    projection: ModelWorkStateResult,
    open_tickets: frozenset[str],
    overlay: ModelWorklistOverlay,
    now: datetime,
) -> tuple[ModelWorklistItemIn, ...]:
    """The in-scope tickets a lane ended without finishing, with a named follow-up and no successor.

    Stopped is the projection's reading: the newest lane on the ticket ended blocked or partial and no lane is live on it.
    A ticket with an open in-scope pull request is not here: the pull request is its successor and sits in the pull request
    class. ``left`` carries the follow-up cells the lane's rows named; with ``stopped_follow_up_cells`` declared, a ticket
    whose lane named none is not a named follow-up and is left out.
    """
    out: list[ModelWorklistItemIn] = []
    for item in projection.items:
        if (
            item.kind is not EnumWorkItemKind.TICKET
            or item.state is not EnumWorkItemState.STOPPED
            or item.scope is not EnumScopeStatus.IN_SCOPE
            or not item.ticket
        ):
            continue
        left = item.left or ""
        if overlay.stopped_follow_up_cells and not any(
            f"{cell}=" in left for cell in overlay.stopped_follow_up_cells
        ):
            continue
        if item.ticket in open_tickets:
            continue
        out.append(
            ModelWorklistItemIn(
                kind="stopped",
                key=item.ticket,
                title=item.title or item.ticket,
                detail=f"{item.reason}; left: {left}" if left else item.reason,
                ticket=item.ticket,
                age_s=None,
            )
        )
    return tuple(out)


def read_worklist(
    *,
    watcher_path: Path,
    controller_state_path: Path,
    projection: ModelWorkStateResult | None,
    scope: ModelGoalScope | None,
    overlay: ModelWorklistOverlay | None,
    excluded_classes: tuple[str, ...],
    conflict_states: tuple[str, ...],
    now: datetime,
) -> tuple[ModelWorklistItemIn, ...] | None:
    """The three classes as items, or ``None`` when the overlay declares none or any input could not be read."""
    if overlay is None:
        return None
    if scope is None or projection is None:
        return None
    owners = read_controller_owners(controller_state_path)
    if owners is None:
        return None
    try:
        prs = json.loads(watcher_path.read_text(encoding="utf-8"))["prs"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    projected = projected_in_scope_prs(projection)
    open_scoped = _open_scoped(prs, scope, projected, excluded_classes)
    open_tickets = frozenset(
        t
        for _, v in open_scoped
        for t in tickets_of(
            str(v.get("facts", {}).get("title", "")),
            str(v.get("facts", {}).get("head_ref", "")),
        )
    )
    return (
        *pr_items(open_scoped, owners, now, conflict_states),
        *stopped_items(projection, open_tickets, overlay, now),
        *shared_red_items(open_scoped, owners, now, overlay.shared_red_after_s),
    )
