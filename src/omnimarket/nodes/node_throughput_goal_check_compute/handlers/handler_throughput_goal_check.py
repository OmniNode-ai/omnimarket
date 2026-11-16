# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hourly check's goal check as a pure compute (OMN-23734).

Behaviour-preserving port of ``goal_tick_fold`` / ``goal_tick_step`` and ``HandlerGoalReport`` of the
retired throughput_tick.py (OMN-19998, OMN-22476, OMN-22868, OMN-22876, OMN-23184). It reads no file,
runs no process and has no clock: the caller supplies the scope, the watcher state, the tick's lines,
the previous record, the controller's admissions and receipt reasons, and ``now``. It returns the
``GOAL`` line, the findings, the record to keep and the check's cells; keeping the record is the record
effect's job. A scope that was not read puts nothing in scope and says why; a watcher state that was not
read measures nothing and says so; both leave ``record`` None.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from omnimarket.nodes.node_throughput_goal_check_compute.models.model_throughput_goal_check import (
    IN_SCOPE,
    PAUSED,
    UNLISTED,
    ModelGoalFinding,
    ModelGoalScope,
    ModelThroughputGoalCheckRequest,
    ModelThroughputGoalCheckResult,
)

GOAL_STAMP = "%Y-%m-%dT%H:%M:%SZ"
GOAL_HISTORY_KEEP = 40
GOAL_MERGES_NAMED = 8  # merges outside the goal the NOTE line names
GOAL_HOLDERS_NAMED = 8  # held pull requests the NOTE line names
GOAL_NO_DECISION = "no-decision"
GOAL_MOVING_REASONS = frozenset({"queued", "armed", "ready", "await_arm", "pending_ci"})
GOAL_FIX_REASON = "worker_running"
_GOAL_UNOWNED = re.compile(
    r"^MISSING (?:repo-status|stuck-owner|floor):(?P<repo>[\w.-]+)(?:@\S+)? \|.*(?:shared_red_unowned|UNOWNED)"
)
GOAL_UNREAD_FIX = (
    "restore plugins/onex-overlays/overlays/goal_scope/overlay.yaml and put its directory's parent in "
    "ONEX_SKILL_OVERLAY_ROOTS; until then nothing is in scope, so the fill and the sweep send nothing"
)


def _goal_stamp(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value), GOAL_STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None


def _goal_merges(state: Mapping[str, Any]) -> dict[str, datetime]:
    """ref -> merged_at for every merge the watcher state holds: its ``merges`` map and its merged pull requests."""
    out: dict[str, datetime] = {}
    merges = state.get("merges")
    for key, m in merges.items() if isinstance(merges, Mapping) else ():
        at = _goal_stamp(m.get("merged_at")) if isinstance(m, Mapping) else None
        if at is not None:
            out[str(key)] = at
    prs = state.get("prs")
    for key, pr in prs.items() if isinstance(prs, Mapping) else ():
        facts = pr.get("facts") if isinstance(pr, Mapping) else None
        at = _goal_stamp(facts.get("merged_at")) if isinstance(facts, Mapping) else None
        if at is not None:
            out[str(key)] = at
    return out


def _goal_green_since(record: Mapping[str, Any]) -> datetime | None:
    """When a green pull request turned green: its newest check-run completion."""
    ci = record.get("ci")
    detail = ci.get("detail") if isinstance(ci, Mapping) else None
    stamps = [
        at
        for row in detail or ()
        if isinstance(row, (list, tuple))
        and len(row) > 2
        and (at := _goal_stamp(row[2])) is not None
    ]
    return max(stamps, default=None)


def _goal_digest(cells: Mapping[str, Any]) -> dict[str, Any]:
    return {
        k: cells[k] for k in ("flipped", "unowned", "waiting", "rate_ok", "open_fell")
    }


def _goal_what_changed(
    previous: Mapping[str, Any] | None, digest: Mapping[str, Any]
) -> str:
    if previous is None:
        return "first check"
    parts: list[str] = []
    for key, now in digest.items():
        before = previous.get(key)
        if before == now:
            continue
        if isinstance(now, list):
            added = sorted(set(now) - set(before or ()))
            gone = sorted(set(before or ()) - set(now))
            parts.append(
                f"{key}"
                + (f" +{','.join(added[:3])}" if added else "")
                + (f" -{','.join(gone[:3])}" if gone else "")
            )
        else:
            parts.append(f"{key} {before}->{now}")
    return "; ".join(parts)


def _short_ref(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def _goal_holders(
    opened: Collection[str], reasons: Mapping[str, str] | None
) -> dict[str, str] | None:
    """Each in-scope open pull request the controller is not moving, with its reason; None when the receipt was not read."""
    if reasons is None:
        return None
    by_short = {_short_ref(str(k)): str(v) for k, v in reasons.items()}
    held: dict[str, str] = {}
    for ref in sorted(opened):
        why = by_short.get(_short_ref(ref), GOAL_NO_DECISION)
        if why not in GOAL_MOVING_REASONS:
            held[ref] = why
    return held


def _goal_trend(previous: Mapping[str, Any] | None, now: int) -> tuple[str, int | None]:
    before = (previous or {}).get("merges")
    if not isinstance(before, int) or isinstance(before, bool):
        return "first", None
    return ("up" if now > before else "down" if now < before else "flat"), before


def merge_scope(
    scope: ModelGoalScope, admissions: Mapping[str, Mapping[str, Any]], ref: str
) -> str:
    """Where a merged pull request stood against the goal: the lists' verdict, and ``in-scope`` for an unlisted
    one the controller's newest admission row admitted. The paused list overrides."""
    where = scope.pr_scope(ref)
    row = admissions.get(ref)
    if where == UNLISTED and row is not None and row.get("admitted") is True:
        return IN_SCOPE
    return where


class HandlerThroughputGoalCheck:
    """The tick's goal check: its findings (level, loop, why, fix) and the ``GOAL`` line."""

    def handle(
        self, request: ModelThroughputGoalCheckRequest
    ) -> ModelThroughputGoalCheckResult:
        if request.scope is None:
            why = request.scope_unread or "no goal scope was given"
            return ModelThroughputGoalCheckResult(
                goal_line=f"GOAL unread | {why[:160]}",
                findings=[
                    ModelGoalFinding(
                        level="unknown",
                        loop="goal-scope",
                        why=f"the goal scope overlay is unread: {why}",
                        fix=GOAL_UNREAD_FIX,
                    )
                ],
            )
        if not isinstance(request.watcher_state, Mapping):
            return ModelThroughputGoalCheckResult(
                goal_line="GOAL unread | no PR watcher state, so no goal condition was measured (the merges finding names the read)"
            )
        return self._report(request, request.scope)

    def _report(
        self, request: ModelThroughputGoalCheckRequest, scope: ModelGoalScope
    ) -> ModelThroughputGoalCheckResult:
        state = request.watcher_state or {}
        now = datetime.strptime(request.now, GOAL_STAMP).replace(tzinfo=UTC)
        admissions = {} if request.admissions_unread else request.admissions
        raw_prs = state.get("prs")
        prs: Mapping[str, Any] = raw_prs if isinstance(raw_prs, Mapping) else {}
        window = timedelta(minutes=request.merge_window_min)
        recent = {
            ref: merge_scope(scope, admissions, ref)
            for ref, at in _goal_merges(state).items()
            if timedelta(0) <= now - at <= window
        }
        merged = sorted(ref for ref, where in recent.items() if where == IN_SCOPE)
        controller_merged = sorted(
            ref for ref in merged if scope.pr_scope(ref) != IN_SCOPE
        )
        paused_merged = sorted(ref for ref, where in recent.items() if where == PAUSED)
        unlisted_merged = sorted(
            ref for ref, where in recent.items() if where == UNLISTED
        )
        opened: list[str] = []
        waiting: list[str] = []
        green = 0
        flipped: list[str] = []
        not_drafted_yet = 0
        ready_outside = 0
        previous = request.previous if isinstance(request.previous, Mapping) else None
        seen_drafted = {str(r) for r in (previous or {}).get("drafted_seen") or ()}
        drafted: set[str] = set()
        for ref, record in prs.items():
            facts = record.get("facts") if isinstance(record, Mapping) else None
            if not isinstance(facts, Mapping) or facts.get("state") != "OPEN":
                continue
            if facts.get("draft"):
                if scope.pr_scope(str(ref)) == PAUSED:
                    drafted.add(str(ref))
                continue
            where = merge_scope(scope, admissions, str(ref))
            if where == IN_SCOPE:
                opened.append(str(ref))
                if (
                    isinstance(record.get("ci"), Mapping)
                    and record["ci"].get("verdict") == "GREEN"
                ):
                    green += 1
                    since = _goal_green_since(record)
                    if since is not None and now - since >= timedelta(
                        minutes=request.green_wait_min
                    ):
                        waiting.append(str(ref))
            elif where == PAUSED:
                if str(ref) in seen_drafted:
                    flipped.append(str(ref))
                else:
                    not_drafted_yet += 1
            else:
                ready_outside += 1
        unowned: list[str] = []
        paused_reds = 0
        for line in request.lines:
            m = _GOAL_UNOWNED.match(line)
            if m is None:
                continue
            if scope.repo_in_scope(m["repo"]):
                unowned.append(line.split(" |", 1)[0].split(" ", 1)[1])
            else:
                paused_reds += 1
        history = [
            list(h)
            for h in (previous or {}).get("open_history") or ()
            if isinstance(h, (list, tuple)) and len(h) == 2
        ]
        floor = now - timedelta(hours=request.open_fall_hours)
        older = [
            n
            for at, n in history
            if (stamp := _goal_stamp(at)) is not None
            and stamp <= floor
            and isinstance(n, int)
        ]
        open_fell = (len(opened) < older[-1]) if older else None
        rate_ok = not (
            len(waiting) >= request.wait_threshold
            and len(merged) < request.rate_per_hour
        )
        cells: dict[str, Any] = {
            "merges": len(merged),
            "open": len(opened),
            "green": green,
            "waiting": sorted(waiting),
            "flipped": sorted(flipped),
            "unowned": sorted(unowned),
            "rate_ok": rate_ok,
            "open_fell": open_fell,
            "ready_outside": ready_outside,
            "paused_reds": paused_reds,
            "not_drafted_yet": not_drafted_yet,
            "paused_merges": paused_merged,
            "unlisted_merges": unlisted_merged,
            "controller_admitted_merges": controller_merged,
        }
        holders = _goal_holders(opened, request.pr_reasons)
        trend, merges_prev = _goal_trend(previous, len(merged))
        no_fix = (
            None
            if holders is None
            else sorted(ref for ref, why in holders.items() if why != GOAL_FIX_REASON)
        )
        cells.update(
            {
                "holders": holders,
                "holders_no_fix": no_fix,
                "merges_trend": trend,
                "merges_prev": merges_prev,
            }
        )
        digest = _goal_digest(cells)
        before = (previous or {}).get("digest")
        changed = before != digest
        said = (
            _goal_what_changed(before if isinstance(before, Mapping) else None, digest)
            if changed
            else "nothing changed"
        )
        m_win = request.merge_window_min
        note = (
            f"goal-scope | in_scope_merges_{m_win}m={len(merged)} "
            f"(controller_admitted_{m_win}m={','.join(controller_merged[:GOAL_MERGES_NAMED]) or 'none'}) in_scope_open={len(opened)} "
            f"(green={green} green_over_{request.green_wait_min}m={len(waiting)}) drafts_flipped_back={len(flipped)} "
            f"shared_reds_unowned={len(unowned)} paused_reds_not_counted={paused_reds} paused_not_drafted_yet={not_drafted_yet} "
            f"ready_outside_scope={ready_outside} paused_merges_{m_win}m={','.join(paused_merged[:GOAL_MERGES_NAMED]) or 'none'} "
            f"unlisted_merges_{m_win}m={','.join(unlisted_merged[:GOAL_MERGES_NAMED]) or 'none'} "
            f"merges_trend={trend}{'' if merges_prev is None else f'(was {merges_prev})'} "
            + (
                "held=unread holders=unread no_fix_in_flight=unread"
                if holders is None or no_fix is None
                else f"held={len(holders)} holders={','.join(f'{ref}:{why}' for ref, why in list(holders.items())[:GOAL_HOLDERS_NAMED]) or 'none'} "
                f"no_fix_in_flight={len(no_fix)}"
            )
        )
        goal = (
            f"GOAL changed={'yes' if changed else 'no'} | merges_{m_win}m={len(merged)} open={len(opened)} "
            f"green_waiting={len(waiting)} flipped_back={len(flipped)} unowned_reds={len(unowned)} "
            f"out_of_scope_merges_{m_win}m={len(paused_merged)} unlisted_merges_{m_win}m={len(unlisted_merged)} "
            f"rate_ok={'yes' if rate_ok else 'NO'} open_fell_{request.open_fall_hours}h="
            f"{'unread' if open_fell is None else 'yes' if open_fell else 'NO'} | {said}"
        )
        open_paused = {
            str(k)
            for k, r in prs.items()
            if isinstance(r, Mapping) and (r.get("facts") or {}).get("state") == "OPEN"
        }
        record = {
            "at": now.strftime(GOAL_STAMP),
            "digest": digest,
            "merges": len(merged),
            "drafted_seen": sorted((seen_drafted | drafted) & open_paused),
            "open_history": [*history, [now.strftime(GOAL_STAMP), len(opened)]][
                -GOAL_HISTORY_KEEP:
            ],
        }
        findings = [ModelGoalFinding(level="note", loop="goal-scope", why=note)]
        if request.admissions_unread:
            findings.append(
                ModelGoalFinding(
                    level="unknown",
                    loop="goal-admissions",
                    why=f"the controller's admission record is unread, so merges it admitted off the lists are not counted: {request.admissions_unread}",
                    fix="make the landing controller's state directory readable (--landing-state-dir names it)",
                )
            )
        for ref in cells["flipped"][:8]:
            findings.append(
                ModelGoalFinding(
                    level="missing",
                    loop=f"goal-flipped:{ref}",
                    why=f"{ref} is open and ready but the draft pause drafted it; a paused pull request flips back only on the operator's say",
                    fix=f"hand {ref} to the draft pause lane to draft it again at its head; only the operator restores a paused pull request",
                )
            )
        return ModelThroughputGoalCheckResult(
            goal_line=goal,
            findings=findings,
            lines=[note],
            record=record,
            changed=changed,
            cells=cells,
        )


__all__ = ["GOAL_UNREAD_FIX", "HandlerThroughputGoalCheck", "merge_scope"]
