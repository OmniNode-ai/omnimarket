# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Gather the facts of one merge-sweep reading from the PR watcher's state (OMN-20676).

Replaces the old reader's GitHub reads. Open PRs, the newest copy of every check name at each
head and the merges come from the watcher's state file, and only when that file is fresh by the
watcher consumers' own rule: schema 1, the read-source fields, a last complete tick at most
``max_age_s`` old, a full resync at most 90 minutes old, at least one open PR, every open PR with a
head and every repository with a default branch. A state that fails is refused with its reason and
no reading is made: GitHub is not the fallback.

The ledger is reduced to the rows that decide who owns a PR (the claims that name an open PR or its
ticket, their closing rows and their lanes' last writes), written as minimal ledger lines, so the
request stays small; the claim-owner rule reads the same answer from the reduction as from the
whole ledger.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omnimarket.handlers import rules_merge_sweep as rules
from omnimarket.models.merge_sweep import (
    ModelMergeSweepClaimCheckRequest,
    ModelMergeSweepReadRequest,
    ModelSweepMerge,
    ModelSweepOpenPr,
    ModelSweepRun,
)

from ..models import (
    FULL_RESYNC_MAX_AGE_S,
    TICKS_READ,
    ModelMergeSweepLoadRequest,
    ModelMergeSweepLoadResult,
)

HOLD_LABEL_PREFIX = "hold" + ":"
ARM_OFF_LABEL = HOLD_LABEL_PREFIX + "auto-merge"
PRODUCTION_GATED_REPOS = frozenset({"onex_change_control", "omninode_infra"})
STATE_SCHEMA = 1


def _stamp(text: object) -> datetime | None:
    try:
        return datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _skips_ci(repo: str, facts: dict[str, Any]) -> str:
    """Why the watcher never reads this PR's CI, or ''."""
    if facts.get("draft"):
        return "draft"
    labels = [str(label) for label in facts.get("labels") or ()]
    if any(
        label.startswith(HOLD_LABEL_PREFIX) and label != ARM_OFF_LABEL
        for label in labels
    ):
        return "hold-family label"
    if facts.get("base") == "main" and repo in PRODUCTION_GATED_REPOS:
        return "production gate"
    return ""


def _runs_of(
    repo: str, facts: dict[str, Any], ci: object
) -> tuple[list[ModelSweepRun] | None, str]:
    """The newest copy of each check name at the head, joined with its job id and start time."""
    sha = facts.get("head_sha")
    if (
        isinstance(ci, dict)
        and ci.get("sha") == sha
        and isinstance(ci.get("runs"), list)
    ):
        if ci.get("verdict") != "NONE" and not ci["runs"] and int(ci.get("total") or 0):
            return None, "state read predates per-name runs"
        if "detail" not in ci:
            return None, "state read predates the per-name job ids"
        by_name = {
            str(row[0]): (row[1], row[2])
            for row in ci["detail"]
            if isinstance(row, list) and len(row) == 3
        }
        try:
            return [
                ModelSweepRun(
                    name=str(name),
                    id=int(by_name[str(name)][0]),
                    started_at=str(by_name[str(name)][1]),
                    status=str(status),
                    conclusion=None if conclusion in (None, "") else str(conclusion),
                )
                for name, status, conclusion, _when in ci["runs"]
            ], ""
        except (KeyError, ValueError) as exc:
            return None, f"state detail unread for {repo}@{str(sha)[:10]}: {exc}"
    skip = _skips_ci(repo, facts)
    if skip:
        return None, f"state: watcher does not read CI of a {skip} PR"
    return None, "state has no check-run read at this head yet"


def _refusal(raw: object, now: datetime, max_age_s: float) -> str:
    """'' when the state is fresh and complete, else the reason it cannot be used."""
    if not isinstance(raw, dict) or raw.get("schema") != STATE_SCHEMA:
        found = raw.get("schema") if isinstance(raw, dict) else "?"
        return f"state schema {found} != {STATE_SCHEMA}"
    if not raw.get("operator") or not raw.get("repos"):
        return "state predates the read-source fields (operator, repos)"
    last = _stamp(raw.get("last_tick"))
    if last is None:
        return "state has no complete tick"
    age = (now - last).total_seconds()
    if age > max_age_s:
        return (
            f"state stale: last complete tick {raw.get('last_tick')} is "
            f"{int(age)}s old (max {int(max_age_s)}s)"
        )
    full = _stamp(raw.get("last_full_resync"))
    if full is None or (now - full).total_seconds() > FULL_RESYNC_MAX_AGE_S:
        return (
            f"state inventory stale: last full resync "
            f"{raw.get('last_full_resync') or 'never'}"
        )
    opened = [
        r
        for r in (raw.get("prs") or {}).values()
        if isinstance(r, dict) and (r.get("facts") or {}).get("state") == "OPEN"
    ]
    if not opened:
        return "state holds zero open PRs"
    headless = [r for r in opened if not (r.get("facts") or {}).get("head_sha")]
    if headless:
        return "open PRs with no head in the state"
    defaults = {
        name
        for name, meta in raw["repos"].items()
        if (meta or {}).get("default_branch")
    }
    missing = {(r.get("facts") or {}).get("repo") for r in opened} - defaults
    if missing:
        return f"no default branch in the state for: {' '.join(sorted(map(str, missing))[:5])}"
    return ""


def reduce_ledger(
    lines: Sequence[str],
    now: datetime,
    needed_prs: set[str],
    needed_tickets: set[str],
) -> list[str]:
    """Minimal ledger lines that give the claim-owner rule the same answers as the whole ledger."""
    horizon = now - timedelta(days=rules.CLAIM_HORIZON_DAYS)
    rows = [
        r
        for r in rules.parse_rows(lines)
        if (rules.parse_ts(r["ts"]) or datetime.min.replace(tzinfo=UTC)) >= horizon
    ]
    claim_lanes: set[str | None] = set()
    claim_stamps: set[str] = set()
    for r in rows:
        if r["type"] == "CLAIM" and (
            rules.row_prs(r["text"]) & needed_prs
            or rules.row_tickets(r["text"]) & needed_tickets
        ):
            claim_stamps.add(r["ts"])
            claim_lanes.add(r.get("lane"))
    out: list[str] = []
    last_write: dict[str, str] = {}
    for r in rows:
        lane = r.get("lane")
        at = rules.parse_ts(r["ts"])
        if lane is not None and lane in claim_lanes and at is not None:
            prev = rules.parse_ts(last_write.get(lane))
            if prev is None or at > prev:
                last_write[lane] = r["ts"]
        text = r["text"]
        handed = [
            h
            for m in rules.HANDED_RE.finditer(text)
            for h in m.group(1).split(",")
            if rules.pr_key(h) in needed_prs
        ]
        supersedes = [s for s in rules.SUPERSEDES_RE.findall(text) if s in claim_stamps]
        closes = [s for s in rules.CLOSES_RE.findall(text) if s in claim_stamps]
        released = [s for s in rules.RE_RE.findall(text) if s in claim_stamps]
        keep = (
            (r["type"] == "CLAIM" and r["ts"] in claim_stamps)
            or (r["type"] == "TERMINAL" and lane in claim_lanes)
            or bool(handed or closes or released)
            or (r["type"] == "CLAIM" and bool(supersedes))
            or (r["type"] == "RELEASE" and bool(released))
        )
        if not keep:
            continue
        cells = [r["ts"], r["type"]]
        if lane:
            cells.append(f"lane={lane}")
        if r["type"] == "CLAIM":
            prs = sorted(rules.row_prs(text))
            tickets = sorted(rules.row_tickets(text))
            if prs:
                cells.append("pr=" + ",".join(prs))
            if tickets:
                cells.append("ticket=" + ",".join(tickets))
        if handed:
            cells.append("handed-off=" + ",".join(handed))
        if r["type"] == "CLAIM":
            cells += [f"supersedes-claim={s}" for s in supersedes]
        cells += [f"closes-claim={s}" for s in closes]
        cells += [f"re={s}" for s in released]
        out.append(" | ".join(cells))
    out += [f"{ts} | STATUS | lane={lane}" for lane, ts in sorted(last_write.items())]
    return out


def _pull_state(raw: dict[str, Any], key: str) -> tuple[str | None, str | None, str]:
    """A PR's state and title from its record or, for a followed merge, its merge record."""
    rec = (raw.get("prs") or {}).get(key)
    if isinstance(rec, dict):
        facts = rec.get("facts") or {}
        return str(facts.get("state") or "") or None, facts.get("title") or "", ""
    merge = (raw.get("merges") or {}).get(key)
    if isinstance(merge, dict):
        return "MERGED", merge.get("title") or "", ""
    return (
        None,
        None,
        f"{key} is not in the state: it keeps open PRs and 14 days of merges",
    )


def _read_ticks(path: str | None) -> list[dict[str, Any]] | None:
    """The last ticks of the controller's log; an unparseable line is an UNKNOWN tick."""
    if not path:
        return None
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()[-TICKS_READ:]
    except OSError:
        return None
    ticks: list[dict[str, Any]] = []
    for line in lines:
        try:
            ticks.append(json.loads(line))
        except ValueError:
            ticks.append({"status": "UNKNOWN", "refusal": "unparseable tick"})
    return ticks


class HandlerMergeSweepLoadFacts:
    """Read the watcher state, the ledger, the tick log and the floors into one reading request."""

    def handle(self, request: ModelMergeSweepLoadRequest) -> ModelMergeSweepLoadResult:
        now = rules.as_utc(request.now)
        try:
            raw = json.loads(Path(request.state_path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ModelMergeSweepLoadResult(
                ok=False, why=f"no state file at {request.state_path}"
            )
        except (OSError, ValueError) as exc:
            return ModelMergeSweepLoadResult(
                ok=False, why=f"state file unreadable: {str(exc)[:160]}"
            )
        why = _refusal(raw, now, request.max_age_s)
        if why:
            return ModelMergeSweepLoadResult(ok=False, why=f"PR-STATE REFUSED: {why}")
        try:
            floors = json.loads(Path(request.floors_path).read_text(encoding="utf-8"))[
                "per_repo"
            ]
            ledger = Path(request.ledger_path).read_text(
                encoding="utf-8", errors="replace"
            )
        except (OSError, ValueError, KeyError) as exc:
            return ModelMergeSweepLoadResult(
                ok=False, why=f"floors or ledger unreadable: {str(exc)[:160]}"
            )

        open_prs: list[ModelSweepOpenPr] = []
        needed_prs: set[str] = set()
        needed_tickets: set[str] = set()
        for rec in (raw.get("prs") or {}).values():
            facts = (rec or {}).get("facts") or {}
            if facts.get("state") != "OPEN":
                continue
            repo, number = str(facts["repo"]), int(facts["number"])
            runs, runs_why = _runs_of(repo, facts, rec.get("ci"))
            title = str(facts.get("title") or "")
            needed_prs.add(f"{repo}#{number}".lower())
            ticket = rules.ticket_of(title)
            if ticket:
                needed_tickets.add(ticket)
            open_prs.append(
                ModelSweepOpenPr(
                    repo=repo,
                    number=number,
                    title=title,
                    base=str(facts.get("base") or ""),
                    head_ref=str(facts.get("head_ref") or ""),
                    draft=bool(facts.get("draft")),
                    runs=runs,
                    runs_why=runs_why,
                    files=[],
                    ready_at=None,
                    facts_unread=["files", "ready_at"],
                )
            )

        start = now - timedelta(minutes=request.window_min)
        merges = [
            ModelSweepMerge(
                repo=str(m["repo"]),
                number=int(m["number"]),
                merged_at=m.get("merged_at") or None,
                files=None,
            )
            for m in (raw.get("merges") or {}).values()
            if isinstance(m, dict)
            and (rules.parse_ts(m.get("merged_at")) or datetime.min.replace(tzinfo=UTC))
            >= start
        ]

        ticks = _read_ticks(request.ticks_path)
        needed_prs |= set(rules.escalations(ticks or []))
        claim: ModelMergeSweepClaimCheckRequest | None = None
        state: str | None = None
        claim_title: str | None = None
        state_why = ""
        if request.claim_check_pr:
            key = rules.pr_key(request.claim_check_pr)
            needed_prs.add(key)
            state, claim_title, state_why = _pull_state(raw, key)
            claim_ticket = rules.ticket_of(claim_title)
            if claim_ticket:
                needed_tickets.add(claim_ticket)
        lines = reduce_ledger(ledger.splitlines(), now, needed_prs, needed_tickets)
        if request.claim_check_pr:
            claim = ModelMergeSweepClaimCheckRequest(
                pr=request.claim_check_pr,
                now=request.now,
                state=state,
                title=claim_title,
                state_why=state_why,
                ledger_lines=lines,
            )
        load1 = request.load1
        cpus = request.cpus
        if load1 is None:
            load1 = os.getloadavg()[0]
        if cpus is None:
            cpus = os.cpu_count()
        return ModelMergeSweepLoadResult(
            ok=True,
            facts=ModelMergeSweepReadRequest(
                now=request.now,
                window_min=request.window_min,
                max_reds=request.max_reds,
                load1=load1,
                cpus=cpus,
                floors={str(k): float(v) for k, v in floors.items()},
                open_prs=open_prs,
                merges=merges,
                ledger_lines=lines,
                ticks=ticks,
            ),
            claim_check=claim,
        )
