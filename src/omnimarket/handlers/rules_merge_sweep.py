# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The merge-sweep reading rules (OMN-20676): pure functions over one reading of the fleet.

A port of the merge-sweep skill's ``sweep_rules`` (written under OMN-20107): product merges against
the per-repo floors, the controller stall, the controller's escalations, ledger claim owners, the
newest-run classification of checks and chain heads. Nothing here reads the network, a file or the
clock; the time arrives as an argument. Each rule names the lesson it carries.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

# ---- shared ----------------------------------------------------------------------------------------

# The repositories a sweep lane may work in. Anything else (omniweb, a parked repo) is skipped.
FLEET_REPOS = frozenset(
    {
        "omnibase_core",
        "omnibase_spi",
        "omnibase_compat",
        "omnibase_infra",
        "omnibase_internal",
        "omniclaude",
        "omniclaude-internal",
        "omnimarket",
        "omnidash",
        "omniintelligence",
        "omninode_infra",
    }
)
# Repositories whose merges are never product merges (lesson 1, RULING 2026-09-29T16:45:14Z).
NON_PRODUCT_REPOS = frozenset(
    {"knowledge-base-internal", "knowledge-base", "onex_change_control"}
)
DEFAULT_BRANCHES = frozenset({"dev", "main"})
LOAD_PER_CORE_BAR = (
    1.0  # lesson 3: above this the lanes run on the lab only; none is withheld
)
STALL_TICKS = 3  # lesson 2
DEAD_AFTER_MIN = 45  # lesson 4: a claim's lane silent this long is stale
CLAIM_HORIZON_DAYS = 7
MAX_PRS_PER_LANE = 5
LEASE_BUDGET_S = 900  # lesson 6

STAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z"


def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def as_utc(now: str | datetime) -> datetime:
    got = now if isinstance(now, datetime) else parse_ts(now)
    if got is None:
        raise ValueError(f"unreadable time {now!r}")
    return got if got.tzinfo else got.replace(tzinfo=UTC)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


_TICKET_RE = re.compile(r"\bOMN-\d+\b")


def ticket_of(title: str | None) -> str | None:
    """The first ticket id a PR title names."""
    match = _TICKET_RE.search(title or "")
    return match.group(0) if match else None


def pr_key(token: str) -> str:
    """``OmniNode-ai/omnimarket#3059`` and ``omnimarket#3059`` are the same key."""
    return str(token).strip().strip("`").split("/")[-1].lower()


def repo_of(key: str) -> str:
    return key.split("#", 1)[0]


# ---- lesson 1: product merges only, per-core-repo floors -------------------------------------------

DOC_EXTS = frozenset({".md", ".rst", ".txt", ".adoc"})
DOC_NAMES = frozenset(
    {
        "CHANGELOG",
        "README",
        "LICENSE",
        "NOTICE",
        "AUTHORS",
        "CONTRIBUTING",
        "CODEOWNERS",
    }
)


def _is_doc_path(path: str) -> bool:
    p = PurePosixPath(path)
    if p.parts and p.parts[0] == "docs":
        return True
    if p.parts and p.parts[0] == ".github":  # a workflow or template is CI, never prose
        return False
    return p.suffix.lower() in DOC_EXTS or p.stem.upper() in DOC_NAMES


def is_docs_only(files: Sequence[str] | None) -> bool:
    """True when every changed file is documentation. An empty or unread list is never docs-only."""
    return files is not None and bool(files) and all(_is_doc_path(f) for f in files)


def product_merges(
    merges: Iterable[Mapping[str, Any]],
    floors: Mapping[str, float],
    now: str | datetime,
    window_min: int = 60,
) -> dict[str, Any]:
    """Count product merges in the window and name every floored repo under its floor.

    A merge whose file list was not read (``files`` None) never counts: an unread fact is not a zero
    that happens to be convenient.
    """
    start = as_utc(now) - timedelta(minutes=window_min)
    excluded = {"non_product_repo": 0, "docs_only": 0, "files_unread": 0}
    by_repo: dict[str, int] = {}
    total = 0
    for m in merges:
        at = parse_ts(m.get("merged_at"))
        if at is None or at < start:
            continue
        total += 1
        repo = str(m.get("repo"))
        if repo in NON_PRODUCT_REPOS:
            excluded["non_product_repo"] += 1
        elif m.get("files") is None:
            excluded["files_unread"] += 1
        elif is_docs_only(m["files"]):
            excluded["docs_only"] += 1
        else:
            by_repo[repo] = by_repo.get(repo, 0) + 1
    under = sorted(r for r, floor in floors.items() if by_repo.get(r, 0) < floor)
    return {
        "window_min": window_min,
        "all": total,
        "product": sum(by_repo.values()),
        "by_repo": dict(sorted(by_repo.items())),
        "excluded": excluded,
        "under_floor": under,
    }


# ---- lesson 2: a controller that dispatched no worker over its last ticks is stalled ----------------


def controller_stall(
    ticks: Sequence[Mapping[str, Any]], n: int = STALL_TICKS
) -> dict[str, Any]:
    """Stalled when the last ``n`` ticks dispatched zero workers, or when fewer than ``n`` were read.

    A refused (DEGRADED with ``refusal``) or UNKNOWN tick dispatched nothing and counts as zero; its
    refusal word is a reason. Too few ticks is never read as healthy.
    """
    last = list(ticks)[-n:]
    reasons: list[str] = []
    workers = 0
    for t in last:
        raw_acts = t.get("actions")
        acts: Mapping[str, Any] = raw_acts if isinstance(raw_acts, Mapping) else {}
        workers += int(acts.get("dispatch_worker") or 0)
        if t.get("refusal"):
            reasons.append(str(t["refusal"]))
        elif str(t.get("status") or "UNKNOWN") == "UNKNOWN":
            reasons.append("UNKNOWN tick")
    if len(last) < n:
        reasons.insert(0, f"fewer than {n} ticks")
        return {
            "stalled": True,
            "workers_last": workers,
            "ticks_read": len(last),
            "reasons": reasons,
        }
    if workers == 0:
        reasons.insert(0, f"zero workers over the last {n} ticks")
    return {
        "stalled": workers == 0,
        "workers_last": workers,
        "ticks_read": len(last),
        "reasons": reasons if workers == 0 else [],
    }


def escalations(ticks: Sequence[Mapping[str, Any]]) -> list[str]:
    """Lesson 8: the PRs the newest tick carrying a degraded list names escalation_exhausted."""
    for t in reversed(list(ticks)):
        if isinstance(t.get("degraded"), list):
            out: list[str] = []
            for d in t["degraded"]:
                if (
                    isinstance(d, Mapping)
                    and d.get("reason") == "escalation_exhausted"
                    and "#" in str(d.get("subject"))
                ):
                    k = pr_key(d["subject"])
                    if k not in out:
                        out.append(k)
            return out
    return []


# ---- lesson 4: claim owners from the ledger ----------------------------------------------------------

ROW_RE = re.compile(
    rf"^\s*(?:[-*] +)?\|?\s*(?P<ts>{STAMP})\s*\|\s*(?P<type>[A-Z-]+)\s*\|(?P<rest>.*)$"
)
FIELD = r"(?:^|[\s|])"
LANE_RE = re.compile(FIELD + r"lane=`?([^\s|;,()`]+)")
PR_FIELD_RE = re.compile(FIELD + r"pr=([^\s|]+)")
TICKET_FIELD_RE = re.compile(FIELD + r"tickets?=([^\s|]+)")
SUPERSEDES_RE = re.compile(r"supersedes[-_]claim=(" + STAMP + ")", re.I)
CLOSES_RE = re.compile(r"closes[-_]claim=(" + STAMP + ")", re.I)
RE_RE = re.compile(FIELD + r"re=(" + STAMP + ")")
HANDED_RE = re.compile(r"handed[-_ ]off=([^\s|]+)", re.I)
OMN_RE = re.compile(r"\bOMN-\d+\b")


def parse_rows(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Ledger rows ``<ts> | <TYPE> | key=value | ...`` as dicts, in file order. Other lines are skipped."""
    rows = []
    for pos, line in enumerate(lines):
        m = ROW_RE.match(line)
        if not m:
            continue
        lane = LANE_RE.search(m.group("rest"))
        rows.append(
            {
                "pos": pos,
                "ts": m.group("ts"),
                "type": m.group("type"),
                "lane": lane.group(1) if lane else None,
                "text": m.group("rest"),
            }
        )
    return rows


def row_prs(text: str) -> set[str]:
    out: set[str] = set()
    for m in PR_FIELD_RE.finditer(text):
        out |= {pr_key(t) for t in m.group(1).split(",") if "#" in t}
    return out


def row_tickets(text: str) -> set[str]:
    out: set[str] = set()
    for m in TICKET_FIELD_RE.finditer(text):
        out |= set(OMN_RE.findall(m.group(1)))
    return out


def claim_owner(
    pr: str,
    ticket: str | None,
    rows: Sequence[Mapping[str, Any]],
    now: str | datetime,
    last_push_at: str | None = None,
    dead_after_min: int = DEAD_AFTER_MIN,
    horizon_days: int = CLAIM_HORIZON_DAYS,
) -> dict[str, Any]:
    """Who owns ``pr`` now: ``none``, ``live`` (lane wrote or pushed within the bar) or ``stale``.

    A CLAIM names the PR by its ``pr=`` list or the PR's ticket by its ``ticket=`` field. It is closed
    by a later TERMINAL of its lane, a later row handing the PR off, a later CLAIM carrying
    ``supersedes-claim=<its ts>``, or ``closes-claim=`` / RELEASE ``re=<its ts>``. A stale owner is taken
    over with ``supersedes-claim=<claim_ts>`` (session-resume semantics).
    """
    t_now = as_utc(now)
    key = pr_key(pr)
    horizon = t_now - timedelta(days=horizon_days)
    bar = t_now - timedelta(minutes=dead_after_min)
    last_write: dict[str, datetime] = {}
    for r in rows:
        at = parse_ts(r["ts"])
        if (
            r.get("lane")
            and at
            and at > last_write.get(r["lane"], datetime.min.replace(tzinfo=UTC))
        ):
            last_write[r["lane"]] = at
    open_claims = []
    for i, c in enumerate(rows):
        if c["type"] != "CLAIM":
            continue
        at = parse_ts(c["ts"])
        if at is None or at < horizon:
            continue
        if key not in row_prs(c["text"]) and not (
            ticket and ticket in row_tickets(c["text"])
        ):
            continue
        later = rows[i + 1 :]
        closed = any(
            (r["type"] == "TERMINAL" and r.get("lane") == c.get("lane"))
            or any(
                pr_key(h) == key
                for m in HANDED_RE.finditer(r["text"])
                for h in m.group(1).split(",")
            )
            or (r["type"] == "CLAIM" and c["ts"] in SUPERSEDES_RE.findall(r["text"]))
            or c["ts"] in CLOSES_RE.findall(r["text"])
            or (r["type"] == "RELEASE" and c["ts"] in RE_RE.findall(r["text"]))
            for r in later
        )
        if not closed:
            open_claims.append(c)
    if not open_claims:
        return {"state": "none"}
    push = parse_ts(last_push_at)
    live = [
        c
        for c in open_claims
        if (last_write.get(c.get("lane") or "") or at_min()) >= bar
        or (push and push >= bar)
    ]
    if live:
        c = live[-1]
        return {"state": "live", "lane": c.get("lane"), "claim_ts": c["ts"]}
    c = open_claims[-1]
    return {
        "state": "stale",
        "lane": c.get("lane"),
        "claim_ts": c["ts"],
        "claims": [x["ts"] for x in open_claims],
    }


def at_min() -> datetime:
    return datetime.min.replace(tzinfo=UTC)


def claim_check_verdict(pr_state: str, owner: Mapping[str, Any]) -> tuple[str, int]:
    """The claim-time recheck (lesson 4): exit 0 only when the PR is open and no live lane owns it."""
    state = str(pr_state or "").upper()
    if state in {"MERGED", "CLOSED"}:
        return (state, 3)
    if state != "OPEN":
        return (f"UNREAD state={pr_state!r}", 4)
    if owner.get("state") == "live":
        return (f"OWNED lane={owner.get('lane')}", 2)
    if owner.get("state") == "stale":
        return (f"STALE supersedes-claim={owner.get('claim_ts')}", 0)
    if owner.get("state") == "none":
        return ("FREE", 0)
    return (f"UNREAD owner={owner.get('state')!r}", 4)


# ---- lessons 5, 9, 14, 17: classify on the newest run of each check name -----------------------------

PASSING = frozenset({"success", "neutral", "skipped"})
ARM_CHECK_RE = re.compile(r"auto[- ]?merge", re.I)
STALE_BASE_SQL_RE = re.compile(r"database domain enforcement", re.I)


def classify_checks(
    runs: Iterable[Mapping[str, Any]], ready_at: str | None, files: Sequence[str] | None
) -> list[dict[str, Any]]:
    """One class and remedy per check name whose newest run is not passing.

    The newest run (by start time, then id) is the one GitHub reads; an older red under a newer pass is
    green, and a cancelled newest run blocks even after an earlier pass. A red run that started before
    the PR's last ``ready_for_review`` is draft-era: update the branch, never rerun it.
    """
    newest: dict[str, Mapping[str, Any]] = {}
    for r in runs:
        cur = newest.get(r["name"])
        if cur is None or (str(r.get("started_at") or ""), int(r.get("id") or 0)) > (
            str(cur.get("started_at") or ""),
            int(cur.get("id") or 0),
        ):
            newest[r["name"]] = r
    ready = parse_ts(ready_at)
    workflow_files = any(str(f).startswith(".github/workflows/") for f in (files or ()))
    out = []
    for name in sorted(newest):
        r = newest[name]
        concl = r.get("conclusion")
        if r.get("status") != "completed":
            cls, remedy = "in-progress", "wait"
        elif concl in PASSING:
            continue
        elif ARM_CHECK_RE.search(name) and workflow_files:
            cls, remedy = "workflow-scope-arm", "merge-on-head-without-arming"
        elif ready and (parse_ts(r.get("started_at")) or ready) < ready:
            cls, remedy = "draft-era", "update-branch"
        elif concl == "cancelled":
            cls, remedy = "cancelled-latest", "rerun-newest-once"
        elif STALE_BASE_SQL_RE.search(name):
            cls, remedy = "stale-base-sql", "update-branch"
        else:
            cls, remedy = "real", "fix"
        out.append(
            {
                "name": name,
                "cls": cls,
                "remedy": remedy,
                "run_id": r.get("id"),
                "conclusion": concl,
                "started_at": r.get("started_at"),
            }
        )
    return out


# ---- lesson 7: chain heads -------------------------------------------------------------------------


def chain_heads(
    prs: Iterable[Mapping[str, Any]], bases: frozenset[str] = DEFAULT_BRANCHES
) -> list[dict[str, Any]]:
    """Open PRs on a default branch whose head branch is the base of other open PRs, with every
    descendant number. A branch cycle is walked once."""
    open_prs = [p for p in prs if str(p.get("state", "OPEN")).upper() == "OPEN"]
    by_base: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for p in open_prs:
        by_base.setdefault((p["repo"], p["base"]), []).append(p)
    out = []
    for p in open_prs:
        if p["base"] not in bases:
            continue
        seen: set[int] = {p["number"]}
        stack = [p["head_ref"]]
        visited_refs: set[str] = set()
        while stack:
            ref = stack.pop()
            if ref in visited_refs:
                continue
            visited_refs.add(ref)
            for child in by_base.get((p["repo"], ref), ()):
                if child["number"] not in seen:
                    seen.add(child["number"])
                    stack.append(child["head_ref"])
        children = sorted(seen - {p["number"]})
        if children:
            out.append({"repo": p["repo"], "number": p["number"], "children": children})
    return sorted(out, key=lambda h: (h["repo"], h["number"]))
