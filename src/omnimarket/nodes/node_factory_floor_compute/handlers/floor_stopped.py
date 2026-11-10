# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stopped lanes of the factory floor (OMN-22934): lanes that ended short with work left, from the ledger rows alone.

A lane is stopped when its newest TERMINAL has an outcome the contract lists (partial, blocked, failed), nothing has
claimed the work since, its ticket is in the goal's scope, and the ending row names a branch, an ask or a next step. A
lane that ended done, or ended short and names nothing, is not stopped. The scope is the caller's fact: an unread scope
lists no lane, because an unread scope is never everything in scope.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

from omnimarket.models.model_factory_floor_input import (
    ModelFactoryFloorRequest,
    ModelFloorLedgerRow,
    ModelFloorStoppedConfig,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.floor_lanes import pr_key
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    ModelFloorStoppedLane,
    ModelFloorStoppedSkip,
)

NAMES_BRANCH = "branch"
NAMES_ASK = "ask"
NAMES_NEXT_STEP = "next-step"
UNREAD_SCOPE_CAUSE = "goal scope unread: no stopped lane is listed"


def _key(row: ModelFloorLedgerRow) -> tuple[datetime, str, str]:
    return row.ts, row.row_id, row.lane


def _said(row: ModelFloorLedgerRow) -> str:
    """What the row says: its text, and its reason when the text does not already begin with it."""
    if row.reason and not row.text.startswith(row.reason):
        return f"{row.reason} {row.text}".strip()
    return row.text or row.reason


def _branch(text: str, cfg: ModelFloorStoppedConfig) -> str:
    for pattern in cfg.branch_patterns:
        found = re.search(pattern, text)
        if found:
            return found.group(1 if found.groups() else 0).rstrip(".,;:)")
    return ""


def _head(text: str, cfg: ModelFloorStoppedConfig) -> str:
    found = re.search(cfg.head_pattern, text)
    return found.group(1) if found else ""


def _names(text: str, branch: str, cfg: ModelFloorStoppedConfig) -> tuple[str, ...]:
    folded = text.casefold()
    names: list[str] = []
    if branch:
        names.append(NAMES_BRANCH)
    if any(marker.casefold() in folded for marker in cfg.ask_markers):
        names.append(NAMES_ASK)
    if any(marker.casefold() in folded for marker in cfg.next_step_markers):
        names.append(NAMES_NEXT_STEP)
    return tuple(names)


def _stem(lane: str, cfg: ModelFloorStoppedConfig) -> str:
    return re.sub(cfg.lane_stem_suffix, "", lane)


def _parents(request: ModelFactoryFloorRequest) -> dict[str, str]:
    """Each ticket the Linear snapshot lists as a child, with the issue that lists it."""
    parents: dict[str, str] = {}
    for issue in request.linear:
        for child in issue.children:
            parents.setdefault(child.upper(), issue.id.upper())
    return parents


def _ticket_in_scope(
    ticket: str, listed: set[str], parents: dict[str, str], max_depth: int
) -> bool:
    """The ticket is listed, or a listed ticket is within ``max_depth`` parent steps above it."""
    seen: set[str] = set()
    current = ticket.upper()
    for _ in range(max_depth + 1):
        if current in listed:
            return True
        seen.add(current)
        current = parents.get(current, "")
        if not current or current in seen:
            return False
    return False


def _in_scope(
    row: ModelFloorLedgerRow,
    request: ModelFactoryFloorRequest,
    lane_rows: Sequence[ModelFloorLedgerRow],
) -> bool:
    scope = request.goal_scope
    if scope is None:
        return False
    listed = {
        ticket.upper()
        for ticket in (*scope.in_scope_tickets, *request.scope_ticket_roots)
    }
    parents = _parents(request)
    depth = request.config.stopped.scope_max_depth
    return any(
        _ticket_in_scope(ticket, listed, parents, depth)
        for ticket in (
            *row.tickets,
            *(t for r in lane_rows for t in r.parent_tickets),
        )
    ) or any(scope.classify(pr) == "in_scope" for pr in row.prs)


def _claimed_since(
    ended: ModelFloorLedgerRow,
    work: frozenset[str],
    branch: str,
    claims: Sequence[ModelFloorLedgerRow],
    cfg: ModelFloorStoppedConfig,
) -> str:
    """The lane that claimed the same work after the ending row: a shared ticket, pull request or branch, or its stem."""
    stem = _stem(ended.lane, cfg)
    umbrella = set(cfg.umbrella_tickets)
    for claim in claims:
        if claim.lane == ended.lane or claim.ts <= ended.ts:
            continue
        shared = (set(claim.tickets) - umbrella) & work
        if (
            shared
            or set(claim.prs) & set(ended.prs)
            or (branch and branch in _said(claim))
            or _stem(claim.lane, cfg) == stem
        ):
            return claim.lane
    return ""


def _row_line(row: ModelFloorLedgerRow, cfg: ModelFloorStoppedConfig) -> str:
    stamp = row.ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    outcome = f" outcome={row.outcome}" if row.outcome else ""
    return f"{stamp} {row.kind}{outcome}: {_said(row)}"[: cfg.row_chars]


def build_stopped(
    request: ModelFactoryFloorRequest,
) -> tuple[tuple[ModelFloorStoppedLane, ...], tuple[ModelFloorStoppedSkip, ...], str]:
    """The stopped lanes, the lanes that ended short inside the window and are not listed, and the unread cause."""
    if request.goal_scope is None:
        return (), (), UNREAD_SCOPE_CAUSE
    cfg = request.config.stopped
    by_lane: dict[str, list[ModelFloorLedgerRow]] = {}
    for row in request.ledger:
        if row.lane:
            by_lane.setdefault(row.lane, []).append(row)
    merged_prs = {
        pr_key(pr.key) for pr in request.watcher.prs if pr.state == "MERGED"
    } | {pr_key(merge.key) for merge in request.watcher.merges}
    merged_branches = {
        merge.head_ref for merge in request.watcher.merges if merge.head_ref
    } | {
        pr.head_ref
        for pr in request.watcher.prs
        if pr.state == "MERGED" and pr.head_ref
    }
    claims = tuple(sorted((r for r in request.ledger if r.kind == "CLAIM"), key=_key))
    umbrella = set(cfg.umbrella_tickets)
    stopped: list[tuple[datetime, ModelFloorStoppedLane]] = []
    skipped: list[ModelFloorStoppedSkip] = []
    for lane, unsorted in sorted(by_lane.items()):
        rows = sorted(unsorted, key=_key)
        terminals = [r for r in rows if r.kind == "TERMINAL"]
        if not terminals:
            continue
        ended = terminals[-1]
        if ended.outcome not in cfg.outcomes:
            continue
        age = max(0, int((request.generated_at - ended.ts).total_seconds() // 60))
        if age > cfg.max_age_minutes:
            continue
        if any(r.kind == "CLAIM" and r.ts > ended.ts for r in rows):
            continue

        def skip(cause: str, lane: str = lane, outcome: str = ended.outcome) -> None:
            skipped.append(
                ModelFloorStoppedSkip(lane=lane, outcome=outcome, cause=cause)
            )

        if not _in_scope(ended, request, rows):
            skip("out-of-scope")
            continue
        text = _said(ended)
        branch = _branch(text, cfg)
        names = _names(text, branch, cfg)
        if not names:
            skip("names-no-branch-ask-or-next-step")
            continue
        if ended.prs and all(pr_key(pr) in merged_prs for pr in ended.prs):
            skip("prs-merged")
            continue
        if branch and branch in merged_branches:
            skip("branch-merged")
            continue
        work = frozenset(set(ended.tickets) - umbrella)
        successor = _claimed_since(ended, work, branch, claims, cfg)
        if successor:
            skip(f"claimed-since:{successor}")
            continue
        tickets = tuple(t for t in ended.tickets if t not in umbrella)
        stopped.append(
            (
                ended.ts,
                ModelFloorStoppedLane(
                    lane=lane,
                    ticket=(tickets or ended.tickets or ("",))[0],
                    tickets=tickets,
                    outcome=ended.outcome,
                    ended_at=ended.ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    age_minutes=age,
                    names=names,
                    branch=branch,
                    head=_head(text, cfg),
                    prs=ended.prs,
                    rows=tuple(_row_line(r, cfg) for r in rows[-cfg.rows_kept :]),
                    report_path=cfg.report_path_template.format(lane=lane),
                ),
            )
        )
    ordered = sorted(stopped, key=lambda pair: (pair[0], pair[1].lane), reverse=True)
    return tuple(lane for _, lane in ordered), tuple(skipped), ""
