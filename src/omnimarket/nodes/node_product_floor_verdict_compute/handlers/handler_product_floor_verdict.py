# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B product-floor alarm decisions, without I/O, clock or episode state.

The caller scans git and reads the watcher, clone-sync and controller files. This
port preserves the old compute/read_waiting/read_controller decisions and the
clean_cell/controller_cell/status_row/print_summary formatting. The caller also
owns ledger appends, deliveries and episode advancement. A missing supplied scan
is explicitly UNKNOWN; controller silence uses the caller's threshold (default 30).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Literal

from omnimarket.nodes.node_product_floor_verdict_compute.models.model_product_floor_verdict import (
    ModelProductFloorControllerFacts,
    ModelProductFloorRepoFacts,
    ModelProductFloorVerdictRequest,
    ModelProductFloorVerdictResult,
    parse_stamp,
)

# These are the old readers' parsing/validation exceptions; actual I/O stays outside.
INPUT_ERRORS = (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError)


def stamp(value: datetime) -> str:
    """Exactly queue_status.stamp."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_cell(value: str) -> str:
    return " ".join(value.replace("|", "/").split())


def read_waiting(
    request: ModelProductFloorVerdictRequest,
    repos: dict[str, float],
    now: datetime,
    max_age: timedelta,
) -> dict[str, int]:
    """Parse the already-read watcher object with the old reader's validation."""
    if request.watcher_unavailable is not None:
        raise OSError(request.watcher_unavailable)
    data = request.watcher
    if data is None:
        raise ValueError("no watcher supplied")
    if now - parse_stamp(data["last_tick"]) > max_age:
        raise ValueError("watcher state is stale")
    waiting = dict.fromkeys(repos, 0)
    for pr in data["prs"].values():
        facts = pr["facts"]
        repo = facts["repo"].split("/")[-1]
        if (
            repo in waiting
            and facts["state"] == "OPEN"
            and not facts.get("draft")
            and pr.get("cls") != "held-excluded"
        ):
            waiting[repo] += 1
    return waiting


def read_controller(
    request: ModelProductFloorVerdictRequest, now: datetime
) -> tuple[ModelProductFloorControllerFacts, str | None]:
    """Parse the caller's nonblank tail in order, retaining partial facts on error."""
    facts = ModelProductFloorControllerFacts()
    error = None
    try:
        if request.controller_unavailable is not None:
            raise OSError(request.controller_unavailable)
        if request.controller_lines is None:
            raise ValueError("no controller lines supplied")
        rows = [json.loads(line) for line in request.controller_lines]
        for row in rows:
            parse_stamp(row["ts"])
            performed = row.get("performed", {})
            workers = sum(
                performed.get(key, 0)
                for key in ("dispatch_worker:spawned", "lab_spawn:spawned")
            )
            if type(workers) is not int or workers < 0:
                raise ValueError("invalid worker count")
            facts.workers_per_tick.append(workers)
            facts.ticks.append(row.get("tick", "-"))
            status = row["status"]
            refusal = row.get("refusal")
            facts.statuses.append(f"{status}({refusal})" if refusal else status)
            facts.degraded.append(row.get("degraded"))
        if not rows:
            raise ValueError("no controller ticks")
        last = rows[-1]
        age = (now - parse_stamp(last["ts"])).total_seconds() / 60
        facts = facts.model_copy(
            update={
                "last_ts": last["ts"],
                "last_age_minutes": age,
                "last_status": last["status"],
                "last_refusal": last.get("refusal"),
            }
        )
        numbers = ",".join(str(n) for n in facts.ticks)
        statuses = ",".join(facts.statuses)
        clock = parse_stamp(last["ts"]).strftime("%H:%MZ")
        facts = facts.model_copy(
            update={
                "detail": (
                    f"landing controller dispatched {sum(facts.workers_per_tick)} workers over its last {len(rows)} ticks "
                    f"(ticks {numbers} ; statuses {statuses}; newest tick {clock}, {age:.0f} min ago)"
                )
            }
        )
        if len(rows) < request.controller_ticks:
            error = f"only {len(rows)} of {request.controller_ticks} controller ticks available"
    except INPUT_ERRORS as exc:
        error = f"cannot read controller ticks: {exc}"
    return facts, error


def controller_cell(facts: ModelProductFloorControllerFacts) -> str:
    age = facts.last_age_minutes
    return clean_cell(
        f"workers/tick={','.join(map(str, facts.workers_per_tick)) or '?'} "
        f"newest={facts.last_ts or '?'} age={'?' if age is None else format(age, '.0f')}m "
        f"status={facts.last_status or '?'} refusal={facts.last_refusal or '-'}"
    )


def status_row(
    ts: str, breaches: dict[str, str], cell: str, lane: str, ticket: str
) -> str:
    cells = [ts, "STATUS", f"lane={lane}", f"ticket={ticket}", "guard=product-floor"]
    cells.append("breached=" + ",".join(sorted(breaches)))
    cells.append("controller=" + cell)
    cells.extend(clean_cell(detail) for _, detail in sorted(breaches.items()))
    return " | ".join(cells)


def summary_lines(
    ts: str,
    per_repo: dict[str, ModelProductFloorRepoFacts],
    breaches: dict[str, str],
    unknowns: dict[str, str],
    controller: ModelProductFloorControllerFacts,
) -> tuple[Literal["BREACH", "UNKNOWN", "OK"], list[str]]:
    """Return the lines print_summary printed, with no printing side effect."""
    verdict: Literal["BREACH", "UNKNOWN", "OK"]
    if breaches:
        verdict = "BREACH"
        headline = (
            f"FLOOR-ALARM BREACH n={len(breaches)} : {','.join(sorted(breaches))}"
        )
    elif unknowns:
        verdict = "UNKNOWN"
        headline = "FLOOR-ALARM UNKNOWN " + ",".join(sorted(unknowns))
    else:
        verdict = "OK"
        headline = f"FLOOR-ALARM OK ts={ts}"
    lines = [
        headline,
        *(facts.detail for facts in per_repo.values()),
        controller.detail,
    ]
    lines.extend(
        f"UNKNOWN {name}: {clean_cell(detail)}"
        for name, detail in sorted(unknowns.items())
    )
    # splitlines matches captured stdout even when a raw controller refusal contains a newline.
    return verdict, "\n".join(lines).splitlines()


class HandlerProductFloorVerdict:
    """Pure product-floor alarm decisions: handle(request) -> typed result."""

    def handle(
        self, request: ModelProductFloorVerdictRequest
    ) -> ModelProductFloorVerdictResult:
        now = parse_stamp(request.now)
        ts = stamp(now)
        unknowns: dict[str, str] = {}
        breaches: dict[str, str] = {}
        floors = {
            repo: floor for repo, floor in (request.floors or {}).items() if floor > 0
        }
        if request.floors_unknown is not None:
            unknowns["floors"] = request.floors_unknown
        per_repo = {
            repo: ModelProductFloorRepoFacts(
                floor=floor, detail=f"{repo}: count UNKNOWN"
            )
            for repo, floor in sorted(floors.items())
        }
        max_age = timedelta(minutes=request.max_input_age_minutes)
        waiting = None
        try:
            waiting = read_waiting(request, floors, now, max_age)
        except INPUT_ERRORS as exc:
            unknowns["waiting"] = f"waiting UNKNOWN: {exc}"
        sync = request.clone_sync
        try:
            if request.clone_sync_unavailable is not None:
                raise OSError(request.clone_sync_unavailable)
            if sync is None:
                raise ValueError("no clone-sync supplied")
            last_run = parse_stamp(sync.last_run) if sync.last_run is not None else None
            if last_run is None or now - last_run > max_age:
                unknowns["clone-sync"] = (
                    f"clone-sync absent or stale: {sync.note or last_run}"
                )
        except INPUT_ERRORS as exc:
            unknowns["clone-sync"] = f"cannot read clone-sync: {exc}"
        if "clone-sync" not in unknowns and sync is not None:
            for repo, facts in per_repo.items():
                if not sync.clones.get(repo, []):
                    unknowns[f"clone:{repo}"] = f"no clone for {repo} in clone-sync log"
                    continue
                if repo in request.scan_errors:
                    unknowns[f"floor:{repo}"] = (
                        f"cannot count {repo}: {request.scan_errors[repo]}"
                    )
                    continue
                scanned = request.scans.get(repo)
                if scanned is None:
                    unknowns[f"floor:{repo}"] = f"cannot count {repo}: no scan supplied"
                    continue
                facts = facts.model_copy(update={"landed_seen": scanned.landed})
                per_repo[repo] = facts
                if not scanned.landed:
                    unknowns[f"floor:{repo}"] = (
                        f"{repo}: positive control failed, zero landed PRs in 7 days"
                    )
                    continue
                count, excluded = scanned.count, scanned.docs
                last = parse_stamp(scanned.last) if scanned.last else None
                rate = count / request.window_hours
                queued = waiting[repo] if waiting is not None else None
                waiting_text = (
                    "UNKNOWN waiting" if queued is None else f"{queued} waiting"
                )
                last_text = (
                    f"{(now - last).total_seconds() / 3600:.1f}h ago"
                    if last
                    else "none in 7 days"
                )
                detail = (
                    f"{repo} merged {count} product PRs in {request.window_hours:g}h "
                    f"({rate:g}/h, floor {facts.floor:g}/h) with {waiting_text}; last product merge {last_text}"
                )
                if rate < facts.floor:
                    if queued == 0:
                        detail = f"idle: {repo} under floor, nothing waiting; {detail}"
                    else:
                        breaches[f"floor:{repo}"] = detail
                per_repo[repo] = facts.model_copy(
                    update={
                        "count": count,
                        "rate": rate,
                        "docs_only": excluded,
                        "last_product_merge": stamp(last) if last else None,
                        "detail": detail,
                    }
                )
        controller, error = read_controller(request, now)
        if error:
            unknowns["controller"] = error
        elif sum(controller.workers_per_tick) == 0:
            breaches["controller"] = controller.detail
        age = controller.last_age_minutes
        if age is not None and age > request.controller_silent_after_minutes:
            controller = controller.model_copy(
                update={
                    "detail": f"controller silent for {age:.0f} min; {controller.detail}"
                }
            )
            breaches["controller"] = controller.detail
        verdict, lines = summary_lines(ts, per_repo, breaches, unknowns, controller)
        cell = controller_cell(controller)
        return ModelProductFloorVerdictResult(
            ts=ts,
            per_repo=per_repo,
            waiting=waiting,
            breaches=breaches,
            unknowns=unknowns,
            controller=controller,
            notes=list(request.scan_notes),
            verdict=verdict,
            summary_lines=lines,
            controller_cell=cell,
            status_row=status_row(
                ts, breaches, cell, request.row_lane, request.row_ticket
            ),
        )
