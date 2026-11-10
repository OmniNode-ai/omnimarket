# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ruling accounting (the refusal gate) and the dropped-decision count."""

from __future__ import annotations

import bisect
import datetime as dt
from collections import Counter
from collections.abc import Mapping, Sequence

from omnimarket.nodes.node_handoff_check_effect.handlers.ledger_rows import (
    holds_in_force,
    open_claims,
    ref_url,
    umbrella_tickets,
)
from omnimarket.nodes.node_handoff_check_effect.models import (
    ModelAccount,
    ModelDecisionRef,
    ModelDroppedDecisions,
    ModelHandoffPolicy,
    ModelLedgerRow,
    ModelRulingGatePolicy,
    fmt_ts,
)


def ruling_items(
    rows: Sequence[ModelLedgerRow], gate: ModelRulingGatePolicy, now: dt.datetime
) -> list[ModelLedgerRow]:
    lo = now - dt.timedelta(hours=gate.window_hours)
    return [r for r in rows if r.type in gate.row_types and lo <= r.ts <= now]


def item_ids(items: Sequence[ModelLedgerRow]) -> list[str]:
    counts = Counter(fmt_ts(r.ts) for r in items)
    seen: Counter[str] = Counter()
    ids = []
    for r in items:
        base = fmt_ts(r.ts)
        seen[base] += 1
        ids.append(base if counts[base] == 1 else f"{base}#{seen[base]}")
    return ids


def derive_status(
    r: ModelLedgerRow,
    after: Sequence[ModelLedgerRow],
    claims: Mapping[str, ModelLedgerRow],
    holds: Sequence[ModelLedgerRow],
    gate: ModelRulingGatePolicy,
    umbrella: set[str],
) -> tuple[str, str]:
    """A status the ledger itself proves. Umbrella tickets never derive one."""
    keys = r.tickets - umbrella
    if not keys:
        return "", ""
    for lane, claim in claims.items():
        if claim.ts >= r.ts and claim.cells.get("ticket") in keys:
            return "in_flight", f"open CLAIM lane={lane} ({ref_url(claim)})"
    for h in holds:
        if h.cells.get("ticket") in keys:
            return "held", f"HOLD id={h.cells['id']} in force ({ref_url(h)})"
    for x in after:
        if (
            x.type == "TERMINAL"
            and x.ts > r.ts
            and x.cells.get("ticket") in keys
            and x.cells.get("outcome") in gate.done_outcomes
        ):
            return (
                "done",
                f"TERMINAL outcome={x.cells['outcome']} lane={x.cells.get('lane', '?')} ({ref_url(x)})",
            )
    return "", ""


def account_rulings(
    rows: Sequence[ModelLedgerRow],
    gate: ModelRulingGatePolicy,
    now: dt.datetime,
    declared: Mapping[str, Mapping[str, object]],
) -> list[ModelAccount]:
    items = ruling_items(rows, gate, now)
    umbrella = umbrella_tickets(rows, gate, now)
    claims, holds, stamps = (
        open_claims(rows),
        holds_in_force(rows),
        [x.ts for x in rows],
    )
    out = []
    for item, r in zip(item_ids(items), items, strict=True):
        acc = ModelAccount(item=item, row=r)
        d = declared.get(item)
        if d is not None:
            acc.status = str(d.get("status", ""))
            acc.evidence = str(d.get("evidence", "")).strip()
            acc.how = "declared"
            if acc.status not in gate.statuses:
                acc.problem = (
                    f"status {acc.status!r} is not one of {', '.join(gate.statuses)}"
                )
            elif not acc.evidence:
                acc.problem = "status declared with no evidence"
        else:
            acc.status, acc.evidence = derive_status(
                r,
                rows[bisect.bisect_right(stamps, r.ts) :],
                claims,
                holds,
                gate,
                umbrella,
            )
            acc.how = "derived from the ledger" if acc.status else ""
            if not acc.status:
                acc.problem = "no status"
        out.append(acc)
    return out


def dropped_decisions(
    rows: Sequence[ModelLedgerRow], policy: ModelHandoffPolicy, now: dt.datetime
) -> ModelDroppedDecisions:
    """Rulings and consents with no lane, PR or finish inside the horizon after them.

    A decision whose horizon has not run out in the data is pending, never dropped.
    """
    gate, dd = policy.ruling_gate, policy.dropped_decision
    umbrella = umbrella_tickets(rows, gate, now)
    horizon = dt.timedelta(hours=dd.hours)
    stamps = [x.ts for x in rows]
    newest = min(max(stamps, default=now), now)
    lo = now - dt.timedelta(hours=dd.scan_hours)
    decisions = [r for r in rows if r.type in gate.row_types and lo <= r.ts <= now]
    result = ModelDroppedDecisions()
    for r in decisions:
        keys = r.tickets - umbrella
        stamp = fmt_ts(r.ts)
        end = r.ts + horizon
        found = ""
        for x in rows[
            bisect.bisect_left(stamps, r.ts) : bisect.bisect_right(stamps, end)
        ]:
            if x is r:
                continue
            cited = stamp in x.body
            related = x.cells.get("ticket") in keys or cited
            if x.type in dd.evidence_row_types and related:
                found = f"{x.type} lane={x.cells.get('lane', '?')} ({ref_url(x)})"
                break
            if related and any(x.cells.get(c) for c in dd.evidence_pr_cells):
                found = f"PR cell on {x.type} ({ref_url(x)})"
                break
        elapsed = end <= newest
        # A ruling whose only ticket is an umbrella cannot be linked to any lane: it is not called dropped.
        none = "dropped" if keys else "unlinkable"
        bucket = (
            ("evidenced" if found else none)
            if elapsed
            else ("pending_evidenced" if found else "pending_no_evidence_yet")
        )
        getattr(result, bucket).append(
            ModelDecisionRef(
                stamp=stamp,
                row_type=r.type,
                ticket=r.cells.get("ticket", "-"),
                evidence=found,
            )
        )
    return result
