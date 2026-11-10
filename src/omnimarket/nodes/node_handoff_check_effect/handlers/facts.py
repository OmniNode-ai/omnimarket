# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factual lines of each handoff section, every one with its source and read time."""

from __future__ import annotations

import datetime as dt
import os
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from omnimarket.nodes.node_handoff_check_effect.handlers.ledger_rows import (
    open_claims,
    ref_url,
)
from omnimarket.nodes.node_handoff_check_effect.handlers.readers import (
    plugin_root,
    read_prs,
    run_reader,
)
from omnimarket.nodes.node_handoff_check_effect.models import (
    ModelFact,
    ModelHandoffPolicy,
    ModelLedgerRow,
    fmt_ts,
)

PR_RE = re.compile(r"[A-Za-z0-9_.-]+#\d+")


def make_fact(
    text: str,
    source: str,
    read_at: str,
    ttl: int = 0,
    probe: str = "",
    val: str = "",
) -> ModelFact:
    return ModelFact(
        text=text, source=source, read_at=read_at, ttl=ttl, probe=probe, val=val
    )


def clip(text: str, limit: int = 240) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def read_lines(path: Path | None) -> list[str]:
    if not path:
        return []
    return [
        ln.strip().lstrip("- ").strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    ]


def parse_plan(
    path: Path, rows: Sequence[ModelLedgerRow], now: dt.datetime
) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    boxes = re.findall(r"^\s*[-*] \[( |x|X)\]", text, re.M)
    done = sum(1 for b in boxes if b.lower() == "x")
    stem = path.stem
    first = next(
        (
            r
            for r in rows
            if r.type in ("RULING", "OPERATOR-CONSENT")
            and (stem in r.raw or path.name in r.raw)
        ),
        None,
    )
    pos = (
        f"{done} of {len(boxes)} tasks done"
        if boxes
        else "task position unreadable (no checkbox tasks in the file)"
    )
    since = (
        f"first ruling {fmt_ts(first.ts)} ({ref_url(first)})"
        if first
        else "no ruling in the ledger names this plan"
    )
    return f"plan {path.name}: {pos}; {since}", fmt_ts(
        dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC)
    )


def lane_facts(
    rows: Sequence[ModelLedgerRow], policy: ModelHandoffPolicy, now: dt.datetime
) -> list[ModelFact]:
    cfg = policy.lanes
    ttl, at = policy.stale.ttl_for("ledger"), fmt_ts(now)
    last: dict[str, dt.datetime] = {}
    for r in rows:
        if r.cells.get("lane"):
            last[r.cells["lane"]] = r.ts
    lo = now - dt.timedelta(days=cfg.claim_scan_days)
    classes: dict[str, list[ModelFact]] = {
        "LIVE": [],
        "QUIET": [],
        "DEAD-CLAIM candidate": [],
    }
    for lane, c in sorted(
        open_claims(rows).items(), key=lambda kv: kv[1].ts, reverse=True
    ):
        if c.ts < lo:
            continue
        age = (now - last[lane]).total_seconds() / 60
        if age <= cfg.live_within_minutes:
            kind, note = "LIVE", f"row in the last {cfg.live_within_minutes} min"
        elif age >= cfg.dead_after_hours * 60:
            kind, note = (
                "DEAD-CLAIM candidate",
                "no row for %.1f h; ledger-inferred, not a liveness verdict"
                % (age / 60),
            )
        else:
            kind, note = "QUIET", "no row for %.1f h" % (age / 60)
        classes[kind].append(
            make_fact(
                f"{lane} ticket={c.cells.get('ticket', '-')} open since {fmt_ts(c.ts)}: {kind} ({note})",
                f"ledger {c.ref}",
                at,
                ttl,
                f"lane:{lane}",
                "open",
            )
        )
    cap = cfg.max_lines_per_class
    out = []
    for kind, facts in classes.items():
        out.append(
            make_fact(
                f"{kind}: {len(facts)} open CLAIM(s) in the last {cfg.claim_scan_days} days"
                + (f"; the newest {cap} follow" if len(facts) > cap else ""),
                "ledger scan",
                at,
                ttl,
            )
        )
        out += facts[:cap]
    return out


def stopped_facts(
    rows: Sequence[ModelLedgerRow], policy: ModelHandoffPolicy, now: dt.datetime
) -> list[ModelFact]:
    cfg = policy.stopped_work
    lo = now - dt.timedelta(hours=cfg.window_hours)
    finished = {
        (r.cells.get("ticket"))
        for r in rows
        if r.type == "TERMINAL"
        and r.cells.get("outcome") in ("done", "merged", "completed")
    }
    stopped = [
        r
        for r in rows
        if r.type == "TERMINAL"
        and lo <= r.ts <= now
        and r.cells.get("outcome") not in cfg.ok_outcomes
        and r.cells.get("ticket") not in finished
    ]
    by = Counter(r.cells.get("outcome", "?") for r in stopped)
    at, ttl = fmt_ts(now), policy.stale.ttl_for("ledger")
    out = [
        make_fact(
            f"{len(stopped)} stopped TERMINALs in the window not since finished: "
            + ", ".join(f"{k}={v}" for k, v in sorted(by.items())),
            "ledger scan",
            at,
            ttl,
        )
    ]
    for r in stopped[-cfg.max_lines :]:
        ptr = (
            " ".join(
                f"{k}={r.cells[k]}"
                for k in ("pr", "branch", "patch", "resume")
                if r.cells.get(k)
            )
            or "no restart pointer on the row"
        )
        out.append(
            make_fact(
                f"{r.cells.get('lane', '?')} ticket={r.cells.get('ticket', '-')} outcome={r.cells.get('outcome')}; restart: re-dispatch the ticket via /omni:remote-lane ({ptr})",
                f"ledger {r.ref}",
                at,
                ttl,
            )
        )
    return out


def ask_facts(
    rows: Sequence[ModelLedgerRow], policy: ModelHandoffPolicy, now: dt.datetime
) -> list[ModelFact]:
    cfg = policy.operator_asks
    lo = now - dt.timedelta(hours=cfg.window_hours)
    acked = {r.cells.get("re") for r in rows if r.type == "ACK"}
    answered = [r for r in rows if r.type in ("RULING", "OPERATOR-CONSENT")]
    asks = [
        r
        for r in rows
        if r.type == "MSG"
        and r.cells.get("to", "").startswith("operator")
        and lo <= r.ts <= now
        and r.cells.get("id") not in acked
        and not any(
            a.ts > r.ts and a.cells.get("ticket") == r.cells.get("ticket")
            for a in answered
        )
    ]
    at, ttl = fmt_ts(now), policy.stale.ttl_for("ledger")
    if not asks:
        return [
            make_fact(
                "no MSG to the operator is waiting without an ACK or a later ruling on its ticket",
                "ledger scan",
                at,
                ttl,
            )
        ]
    return [
        make_fact(
            f"from {r.cells.get('from', '?')} on {fmt_ts(r.ts)}: {clip(r.text)}",
            f"ledger {r.ref}",
            at,
            ttl,
        )
        for r in asks[-cfg.max_lines :]
    ]


def pr_facts(
    rows: Sequence[ModelLedgerRow],
    policy: ModelHandoffPolicy,
    now: dt.datetime,
    extra: Sequence[str],
    reads: Path | None,
) -> tuple[list[ModelFact], list[str]]:
    cfg = policy.prs
    lo = now - dt.timedelta(hours=cfg.window_hours)
    named: dict[str, dt.datetime] = {}
    for r in rows:
        if (
            r.type == "MSG"
            and r.cells.get("to") == "landing-controller"
            and lo <= r.ts <= now
        ):
            for p in PR_RE.findall(r.cells.get("pr", "")):
                named[p] = r.ts
    prs = [
        *extra,
        *[
            p
            for p, _ in sorted(named.items(), key=lambda kv: kv[1], reverse=True)
            if p not in extra
        ],
    ][: cfg.max_prs]
    if not prs:
        return [
            make_fact(
                "no PR named in the window",
                "ledger scan",
                fmt_ts(now),
                policy.stale.ttl_for("ledger"),
            )
        ], []
    state, src, at = read_prs(prs, reads, policy, now)
    ttl = policy.stale.ttl_for("pr-watcher")
    groups: dict[str, list[ModelFact]] = {
        "MERGED": [],
        "RUNNING": [],
        "CLOSED": [],
        "UNREAD": [],
    }
    unverified = []
    for p in prs:
        rec = state.get(p)
        if not rec:
            groups["UNREAD"].append(
                make_fact(
                    f"{p}: state unreadable", src, at, ttl, f"pr:{p}", "unreadable"
                )
            )
            unverified.append(f"PR {p} state could not be read from the watcher")
            continue
        s = rec.get("state", "?")
        val = f"state={s} ci={rec.get('ci', '?')}"
        g = "MERGED" if s == "MERGED" else "CLOSED" if s == "CLOSED" else "RUNNING"
        groups[g].append(
            make_fact(
                f"{p}: {val} head={rec.get('head', '?')[:12]} armed={rec.get('armed', '?')} ci_read={rec.get('read_at', '?')}",
                src,
                at,
                ttl if g == "RUNNING" else 0,
                f"pr:{p}",
                val,
            )
        )
    facts = []
    for g, items in groups.items():
        facts.append(make_fact(f"{g}: {len(items)} PR(s)", src, at, ttl))
        facts.extend(items)
    return facts, unverified


def limit_facts(
    rows: Sequence[ModelLedgerRow],
    policy: ModelHandoffPolicy,
    now: dt.datetime,
    declared: Sequence[str],
) -> list[ModelFact]:
    lo = now - dt.timedelta(hours=policy.limits.window_hours)
    lifts = Counter(
        r.cells["re"]
        for r in rows
        if r.type == "RELEASE"
        and lo <= r.ts <= now
        and re.match(r"^20\d\d-\d\d-\d\dT[\d:]+Z$", r.cells.get("re", ""))
    )
    at, ttl = fmt_ts(now), policy.stale.ttl_for("ledger")
    out = [
        make_fact(
            f"freeze/hold {k} is still lifted case by case: {n} scoped RELEASE row(s) in the window",
            "ledger scan of RELEASE re=",
            at,
            ttl,
        )
        for k, n in sorted(lifts.items())
    ]
    out += [
        make_fact(f"{d} (declared by the writer)", "writer", at, 0) for d in declared
    ]
    return out or [
        make_fact(
            "no limit found in the ledger window and none declared",
            "ledger scan",
            at,
            ttl,
        )
    ]


def reader_excerpts(lane: str, now: dt.datetime) -> list[ModelFact]:
    root = plugin_root()
    if not root or not os.environ.get("ONEX_LEDGER_PATH"):
        return []
    out = []
    for label, script, args in (
        ("catchup holds", "ledger-catchup/catchup.sh", ["24"]),
        ("inbox", "ledger-msg/inbox.sh", [lane] if lane else []),
    ):
        path = root / "skills" / script
        if label == "inbox" and not lane:
            continue
        rc, text = (
            run_reader(["bash", str(path), *args])
            if path.is_file()
            else (127, "reader not found")
        )
        out.append(
            make_fact(
                f"{label}: "
                + (
                    clip(" / ".join(text.splitlines()[-3:]), 300)
                    if rc in (0, 3)
                    else f"UNREADABLE rc={rc}"
                ),
                f"{script} {' '.join(args)}",
                fmt_ts(now),
                3600,
            )
        )
    return out
