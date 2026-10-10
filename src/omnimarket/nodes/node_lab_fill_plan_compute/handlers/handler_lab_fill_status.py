# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Explain zero launches and idle capacity from one snapshot so unread work stays visible."""

import re
from collections.abc import Mapping, Sequence

from ..models.model_lab_fill_status import (
    ModelLabFillStatusRequest,
    ModelLabFillStatusResult,
)
from .handler_lab_fill_dispatch_plan import APPROVED_KINDS, KINDS
from .helpers_js_value import UNDEFINED, is_integer, truthy
from .helpers_outcomes import number, obj, string, text


def cell(value: object) -> str:
    """Ledger separators in a reading must not create extra cells."""
    return re.sub(r"[|\n\r]", "/", string(value)).strip()


def count_of(items: Sequence[Mapping[str, object]]) -> int:
    """A grouped skip counts its positive integer n; every other entry counts once."""
    return sum(
        int(number(i.get("n")))
        if is_integer(i.get("n")) and number(i.get("n")) > 0
        else 1
        for i in items
    )


def reason_counts(items: Sequence[Mapping[str, object]]) -> str:
    """Frequency first, lexical ties, with colon details grouped under their reason."""
    counts: dict[str, int] = {}
    for i in items:
        key = (text(i.get("reason")) or "unknown").split(":")[0] or "unknown"
        counts[key] = counts.get(key, 0) + count_of([i])
    return ",".join(
        f"{k}:{n}" for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )


def _launched(p: Mapping[str, object]) -> bool:
    return (
        re.match(r"^(not-started|skipped:)", string(p.get("outcome", UNDEFINED)))
        is None
    )


class HandlerLabFillStatus:
    """Build the exact post-timestamp cells and the workflow's idle object."""

    def handle(self, request: ModelLabFillStatusRequest) -> ModelLabFillStatusResult:
        cfg, capacity, selection = request.config, request.capacity, request.selection
        chosen = request.candidates
        candidates = chosen.candidates if chosen else ()
        diagnostics = chosen.source_diagnostics if chosen else {}
        ps, fb = request.placements, request.fallbacks
        all_ps = (*ps, *fb)
        reasons = (*selection.skipped, *selection.deferred)

        def omitted(kinds: Sequence[str]) -> float:
            return (
                sum(number(chosen.not_emitted.get(k)) for k in kinds) if chosen else 0
            )

        def kind_reasons(kind: str, idle: bool) -> str:
            d = obj(diagnostics.get(kind))
            seen = sum(c.get("kind") == kind for c in candidates)
            grouped = reason_counts([c for c in reasons if c.get("kind") == kind])
            source, detail = (chosen.source, chosen.detail) if chosen else ("none", "")
            if idle:
                fallback = (
                    "not-read:no-headroom"
                    if capacity.budget == 0
                    else "unreadable:" + (detail or source)
                    if source in ("none", "cache-stale")
                    else "ok:" + source
                    if seen
                    else "unreadable:enumerate-partial"
                    if source == "enumerate-partial"
                    else "empty:" + source
                )
            else:
                fallback = (
                    "not-read:no-headroom"
                    if selection.budget == 0
                    else detail or source
                    if chosen
                    else "not-read"
                )
            read = text(d.get("read")) or fallback
            seen_value = d.get("seen") if d.get("seen") is not None else seen
            eligible_value = (
                d.get("eligible") if d.get("eligible") is not None else seen
            )
            return cell(
                f"{kind}:{read}:seen={string(seen_value)}:eligible={string(eligible_value)}"
                + (":" + string(d["reasons"]) if truthy(d.get("reasons")) else "")
                + (":" + grouped if grouped else "")
            )

        def tiers(idle: bool) -> str:
            return (
                "tier1-m4-sprint:"
                + ",".join(
                    kind_reasons(k, idle) for k in KINDS if k not in APPROVED_KINDS
                )
                + ";tier2-approved:"
                + ",".join(kind_reasons(k, idle) for k in APPROVED_KINDS)
            )

        host_cell = (
            ",".join(
                f"{h.name}:{h.lanes}"
                + ("" if h.lanes else f"({h.reason or 'none'})")
                + f"/run{h.running}"
                + ("/cap-bound" if h.cap_bound else "")
                for h in capacity.hosts
            )
            or "none"
        )
        lanes = (
            ",".join(
                f"{string(p.get('lane', UNDEFINED))}@{string(p.get('host', UNDEFINED))}:{string(p.get('outcome', UNDEFINED))}"
                for p in all_ps
            )
            or "none"
        )
        launched = sum(_launched(p) for p in all_ps)
        dispatched_lanes = {text(p.get("lane")) for p in ps if _launched(p)}
        engines = (
            ",".join(
                [
                    *(
                        f"{string(p.get('lane', UNDEFINED))}:{text(p.get('engine')) or 'unknown'}"
                        for p in ps
                    ),
                    *(
                        f"{string(f.get('lane', UNDEFINED))}:{string(f.get('engine', UNDEFINED))}"
                        for f in fb
                        if _launched(f)
                    ),
                ]
            )
            or "none"
        )
        fallback_parts = []
        for i in selection.dispatch:
            if truthy(i.get("fallback")):
                f = obj(i["fallback"])
                fallback_parts.append(
                    f"{string(i.get('lane', UNDEFINED))}:{string(f.get('from', UNDEFINED))}->{string(i.get('engine', UNDEFINED))}@{string(i.get('host', UNDEFINED))}:preflight:{string(f.get('reason', UNDEFINED))}"
                )
        fallback_parts.extend(
            f"{string(f.get('from_lane', UNDEFINED))}:{string(f.get('from_engine', UNDEFINED))}->{string(f.get('engine', UNDEFINED))}@{string(f.get('host', UNDEFINED))}:{string(f.get('outcome', UNDEFINED))}:{string(f.get('reason', UNDEFINED))}"
            for f in fb
        )
        cells = [
            "STATUS",
            "lane=lab-fill",
            f"ticket={cfg.parent_ticket}",
            "actor=launchd:lab-fill",
            "model=sonnet",
            f"run={cfg.run_key}",
            f"parent={cfg.parent_lane}",
            f"pillar={cfg.pillar}",
            "hosts=" + cell(host_cell),
            f"budget={selection.budget}",
            "candidates="
            + cell(chosen.source)
            + ":"
            + string(len(candidates) + omitted(APPROVED_KINDS))
            + (":" + cell(chosen.detail) if chosen.detail else "")
            if chosen
            else "candidates=not-read:0",
            f"dispatched={launched}",
        ]
        cells.extend(
            f"tier{2 if approved else 1}-candidates={string(sum((c.get('kind') in APPROVED_KINDS) == approved for c in candidates) + (omitted(APPROVED_KINDS) if approved else 0))}"
            for approved in (False, True)
        )
        cells.extend(
            f"tier{2 if approved else 1}-dispatched={sum(text(c.get('lane')) in dispatched_lanes and (c.get('kind') in APPROVED_KINDS) == approved for c in selection.dispatch)}"
            for approved in (False, True)
        )
        cells.extend(
            [
                f"placed={sum(p.get('outcome') == 'placed' for p in all_ps)}",
                f"skipped={count_of(selection.skipped)}",
                f"deferred={count_of(selection.deferred)}",
            ]
        )
        if chosen and chosen.open_count_gate is not None:
            gate = chosen.open_count_gate
            cells.append(
                cell(
                    "open-count="
                    + (text(gate.get("trend")) or "unknown")
                    + (
                        ":" + string(gate["detail"])
                        if truthy(gate.get("detail"))
                        else ""
                    )
                    + (":gated" if gate.get("gated") is True else "")
                    + (
                        ":trend-file=" + string(gate["trend_read"])
                        if truthy(gate.get("trend_read")) and gate["trend_read"] != "ok"
                        else ""
                    )
                )
            )
        if launched == 0:
            why = ",".join(
                filter(
                    None,
                    (
                        reason_counts(selection.skipped),
                        reason_counts(selection.deferred),
                    ),
                )
            )
            cells.append(
                "skip-reasons="
                + cell(why or ("unplaced" if candidates else "no-candidates"))
            )
            if chosen and chosen.filtered is not None:
                cells.append(
                    f"prefiltered=owned:{string(number(chosen.filtered.get('owned')))},fenced:{string(number(chosen.filtered.get('fenced')))}"
                )
            cells.extend(
                [
                    "source-reasons=" + ";".join(kind_reasons(k, False) for k in KINDS),
                    "tier-reasons=" + tiers(False),
                ]
            )
        cells.extend(
            [
                "engines=" + cell(engines),
                "fallbacks=" + cell(",".join(fallback_parts) or "none"),
                "cap-bound="
                + cell(
                    ",".join(h.name for h in capacity.hosts if h.cap_bound) or "none"
                ),
                "lanes=" + cell(lanes),
            ]
        )

        # A setup failure, refusal or deferral did not take a slot; an accepted fallback did.
        def dead(p: Mapping[str, object]) -> bool:
            return (
                p.get("outcome") == "refused"
                or re.match(
                    r"^(engine-failed:|deferred:)", string(p.get("outcome", UNDEFINED))
                )
                is not None
            )

        primary = [
            d
            for d in request.dispatched
            if truthy(d.get("detached"))
            and not any(p.get("lane") == d.get("lane") and dead(p) for p in ps)
        ]
        took = [
            f
            for f in fb
            if f.get("outcome") in ("placed", "pending", "placed-elsewhere")
        ]
        accepted = [*primary, *took]

        def host_of(d: Mapping[str, object]) -> object:
            return next((p for p in ps if p.get("lane") == d.get("lane")), d).get(
                "host"
            )

        hosts = [
            {
                **h.model_dump(mode="json", by_alias=True, exclude_unset=True),
                "free": max(0, h.lanes - sum(host_of(d) == h.name for d in accepted)),
            }
            for h in capacity.hosts
        ]
        free = sum(int(h["free"]) for h in hosts)
        known = all(
            is_integer(obj(diagnostics.get(k)).get("seen"))
            and re.search(
                r"unreadable|not-read",
                text(obj(diagnostics.get(k)).get("read")) or "not-read",
            )
            is None
            for k in APPROVED_KINDS
        )
        depth = (
            sum(number(obj(diagnostics.get(k)).get("seen")) for k in APPROVED_KINDS)
            if known
            else request.probed_approved_depth
            if is_integer(request.probed_approved_depth)
            and number(request.probed_approved_depth) >= 0
            else None
        )
        eligible = max(
            len(candidates),
            sum(number(obj(d).get("eligible")) for d in diagnostics.values()),
        )
        available = max(0, eligible - len(accepted))
        alarm: object = (
            free > 0 and len(accepted) == 0 if truthy(request.apply) else request.apply
        )
        dispatch_reasons = ",".join(
            cell(
                f"{string(d.get('lane', UNDEFINED))}:{text(d.get('skipped')) or text(d.get('detail')) or 'not-started'}"
            )
            for d in request.dispatched
            if not truthy(d.get("detached"))
        )
        placement_reasons = ",".join(
            cell(p.get("detail", UNDEFINED))
            for p in ps
            if p.get("outcome") == "refused"
        )
        idle_hosts = (
            ",".join(
                f"{string(h['name'])}:{h['free']}"
                for h in hosts
                if number(h["free"]) > 0
            )
            or "none"
        )
        details = f"idle-hosts={cell(idle_hosts)} | free-slots={free} | tier-reasons={tiers(True)} | dispatch-reasons={cell(dispatch_reasons or placement_reasons or 'none')}"
        session_hosts = (
            ",".join(
                f"{cell(h['name'])}:load={string(h.get('loadPerCore')) if h.get('loadPerCore') is not None else 'UNKNOWN'}/running={h['running']}/free={h['free']}"
                for h in hosts
            )
            or "load=UNKNOWN"
        )
        session = (
            f"lab {session_hosts} running-lanes={sum(h.running for h in capacity.hosts)} free-slots={free} approved-work={string(depth) if depth is not None else 'UNKNOWN'}"
            + (" IDLE" if free > 0 and available > 0 else "")
        )
        idle = {
            "version": 1,
            "workflow": "lab-fill",
            "fire_id": request.fire_id,
            "run_key": cfg.run_key,
            "observed_at": request.observed_at,
            "hosts": hosts,
            "free_slots": free,
            "dispatched": len(accepted),
            "approved_work_depth": depth,
            "available_work": available,
            "idle_alarm": alarm,
            "friction_recorded": False,
            "friction_cells": f"FRICTION | lane=lab-fill | ticket={cfg.parent_ticket} | actor=launchd:lab-fill | run={cfg.run_key} | class=lab-idle | cost=est {free * 20} lane-minutes (free slots x next 20m interval) | {details}"
            if truthy(alarm)
            else "",
            "alert_text": f"LAB IDLE run={cfg.run_key} {details}"
            if truthy(alarm)
            else "",
            "session_line": session,
        }
        return ModelLabFillStatusResult(cells=tuple(cells), idle=idle)
