# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Assemble a handoff from the ledger, the PR watcher reader and the live probes."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

from omnimarket.nodes.node_handoff_check_effect.handlers.facts import (
    ask_facts,
    lane_facts,
    limit_facts,
    make_fact,
    parse_plan,
    pr_facts,
    read_lines,
    reader_excerpts,
    stopped_facts,
)
from omnimarket.nodes.node_handoff_check_effect.handlers.ledger_rows import (
    holds_in_force,
    load_rows,
)
from omnimarket.nodes.node_handoff_check_effect.handlers.readers import (
    probe_results,
)
from omnimarket.nodes.node_handoff_check_effect.handlers.rulings import (
    account_rulings,
    dropped_decisions,
)
from omnimarket.nodes.node_handoff_check_effect.models import (
    ModelAssembledHandoff,
    ModelFact,
    ModelHandoffCheckRequest,
    ModelHandoffPolicy,
    fmt_ts,
    parse_ts,
)


def now_for(request: ModelHandoffCheckRequest) -> dt.datetime:
    return (
        parse_ts(request.now)
        if request.now
        else dt.datetime.now(dt.UTC).replace(microsecond=0)
    )


def _declared_statuses(path: str | None) -> Mapping[str, Mapping[str, object]]:
    if not path:
        return {}
    loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("statuses: the file must hold a JSON object of item -> record")
    return {str(k): v for k, v in loaded.items() if isinstance(v, dict)}


def assemble(
    request: ModelHandoffCheckRequest, policy: ModelHandoffPolicy, ledger_path: str
) -> ModelAssembledHandoff:
    now = now_for(request)
    ledger = Path(ledger_path)
    rows = load_rows(
        ledger,
        Path(request.archive_dir) if request.archive_dir else ledger.parent / "archive",
    )
    rows = [r for r in rows if r.ts <= now]
    gate = policy.ruling_gate
    declared = _declared_statuses(request.statuses)
    accounts = account_rulings(rows, gate, now, declared)
    refused = [
        f"{a.item} {a.row.type} ticket={a.row.cells.get('ticket', '-')} ({a.row.ref}): {a.problem}"
        for a in accounts
        if a.problem
    ]
    for k in declared:
        if k not in {a.item for a in accounts}:
            refused.append(
                f"{k}: a declared status names no ruling or consent in the window"
            )
    newest = rows[-1].ts if rows else now
    at, ledger_ttl = fmt_ts(now), policy.stale.ttl_for("ledger")
    ledger_src = f"ledger {ledger.name} newest row {fmt_ts(newest)}"
    sections: dict[str, list[ModelFact]] = {s.id: [] for s in policy.sections}
    session = re.sub(
        r"[^A-Za-z0-9_.-]",
        "-",
        request.session or os.environ.get("ONEX_LANE") or "session",
    )
    sections["session"] = [
        make_fact(f"session {session}; handoff assembled {at}", "writer", at, 0)
    ]
    goal_file = Path(request.goal_file) if request.goal_file else None
    goal = read_lines(goal_file)
    sections["goal"] = [
        make_fact(g, f"operator words ({goal_file.name})", at, 0)
        for g in goal
        if goal_file
    ]
    unverified = read_lines(
        Path(request.unverified_file) if request.unverified_file else None
    )
    if not goal:
        unverified.append(
            "goal: no operator-words file given, so this handoff states no goal"
        )
    for plan in request.plans:
        text, mtime = parse_plan(Path(plan), rows, now)
        sections["plans"].append(
            make_fact(
                text,
                f"plan file {Path(plan).name}",
                mtime,
                policy.stale.ttl_for("plan-file"),
            )
        )
    if not request.plans:
        unverified.append("plans: no --plan file given, so no plan position is stated")
    for a in accounts:
        status = a.status or "UNACCOUNTED"
        sections["rulings"].append(
            make_fact(
                f"{a.item} {a.row.type} ticket={a.row.cells.get('ticket', '-')} status={status} ({a.how or 'none'}); evidence: {a.evidence or '-'}",
                f"ledger {a.row.ref}",
                at,
                ledger_ttl,
                "",
                f"status={status}",
            )
        )
    if not accounts:
        sections["rulings"].append(
            make_fact(
                "no RULING or OPERATOR-CONSENT row in the window",
                ledger_src,
                at,
                ledger_ttl,
            )
        )
    held = holds_in_force(rows)
    live = [
        h
        for h in held
        if not (
            re.match(r"^20", h.cells.get("until", ""))
            and parse_ts(h.cells["until"]) < now
        )
    ]
    sections["holds"].append(
        make_fact(
            f"{len(held)} HOLD row(s) have no RELEASE in the whole ledger: {len(live)} in force (until= ahead or open-ended), {len(held) - len(live)} past their until=",
            f"ledger scan {ledger.name} and archive",
            at,
            ledger_ttl,
        )
    )
    for h in live[-policy.holds.max_lines :]:
        sections["holds"].append(
            make_fact(
                f"HOLD id={h.cells['id']} ticket={h.cells.get('ticket', '-')} until={h.cells.get('until', 'open-ended')} (placed {fmt_ts(h.ts)})",
                f"ledger {h.ref}",
                at,
                ledger_ttl,
            )
        )
    sections["lanes"] = lane_facts(rows, policy, now) or [
        make_fact("no open CLAIM in the scan window", ledger_src, at, ledger_ttl)
    ]
    if request.readers:
        sections["lanes"] += reader_excerpts(request.lane or "", now)
    sections["stopped"] = stopped_facts(rows, policy, now)
    sections["asks"] = ask_facts(rows, policy, now)
    sections["prs"], pr_unv = pr_facts(
        rows,
        policy,
        now,
        request.prs,
        Path(request.pr_reads) if request.pr_reads else None,
    )
    unverified += pr_unv
    sections["limits"] = limit_facts(
        rows,
        policy,
        now,
        read_lines(Path(request.limits_file) if request.limits_file else None),
    )
    for rec in probe_results(
        policy, Path(request.probes) if request.probes else None, now
    ):
        sections["services"].append(
            make_fact(
                f"{rec.name}: {rec.verdict} ({rec.detail})",
                f"probe {rec.probe}",
                rec.read_at,
                policy.stale.ttl_for("probe"),
                f"svc:{rec.name}",
                rec.verdict,
            )
        )
        if rec.healthy is not True:
            unverified.append(
                f"service {rec.name} is {rec.verdict} at {rec.read_at}: {rec.detail}"
            )
    stale = sum(f.stale(now) for fs in sections.values() for f in fs)
    for fs in sections.values():
        unverified += [
            f"STALE read: {f.text[:90]} (read {f.read_at})" for f in fs if f.stale(now)
        ]
    sections["unverified"] = [
        make_fact(u, "writer or check", at, 0) for u in unverified
    ] or [make_fact("nothing declared unverified", "writer", at, 0)]
    dropped = dropped_decisions(rows, policy, now)
    lines = [
        f"# Session handoff {session}",
        "",
        f"contract: handoff contract v{policy.contract_version}; assembled {at}; {ledger_src}",
        "",
    ]
    for s in policy.sections:
        lines += [
            f"## {s.title}",
            "",
            *[f.render(now) for f in sections[s.id]],
            "",
        ]
    lines += ["## Dropped-decision accounting", "", dropped.summary_line(), ""]
    stamp = now.strftime("%Y-%m-%dT%H%MZ")
    return ModelAssembledHandoff(
        text="\n".join(lines),
        refused=refused,
        dropped=dropped,
        stale_lines=stale,
        filename=policy.output.filename.format(stamp=stamp, session=session),
    )
