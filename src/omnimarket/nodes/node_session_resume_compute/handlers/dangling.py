# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every CLAIM row no later row closes (session-resume step 2), and whose each one is.

Read-only. Files read, in this order (the order sets which row is "later"):
``<dirname of the ledger>/archive/ROLLING_WORK_LEDGER_*.md`` sorted by name, then the ledger.

A CLAIM is closed when a later row names it: a TERMINAL (or a legacy type mapped to it) whose
``closes-CLAIM=`` names it; a TERMINAL naming no claim whose ``claim-lane=`` (else ``lane=``) is the
claim's lane; a CLAIM whose ``supersedes-claim=`` names it; a RELEASE whose ``re=`` names it.

A reference names a claim, strongest first: an LCT1 claim token (bound by its row digest), a claim
id ``<stamp>-<lane>``, ``lane=<lane>[,<stamp>]``, a live-ledger line pointer bound only to a related
claim, or a bare stamp bound by tier (``claim-lane=``, ``lane=``, a ``-r`` relaunch lane, a shared
ticket); a tier that matches two claims binds none and is reported AMBIGUOUS.

Ownership. Each CLAIM carries the principal the ledger bus stamped on it (``principal=``). In an
operator session every CLAIM is ``own``, as before. In a member session a CLAIM is ``own`` only when
every principal it carries is one of the member's, or, when it carries none, when its ``parent=``
names a lane whose every CLAIM the member's principals wrote (a child of the member's predecessor
session). Everything else, including an unattributed CLAIM with no such parent, is ``foreign``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from omnimarket.session_scope import ModelSessionScope

from ..models.model_session_resume import ModelDanglingClaim, Owner
from .ledger_grammar import (
    LEGACY_TYPE_MAP,
    STAMP,
    Row,
    claim_row_digest,
    opens_a_row,
    parse_row,
)

ARCHIVE_GLOB = "ROLLING_WORK_LEDGER_*.md"
STAMP_RE = re.compile(rf"^{STAMP}$")
STAMP_SEARCH = re.compile(STAMP)
TOKEN_RE = re.compile(r"LCT1-(\d+)-(\d+)-([0-9a-f]{12})-\S+")
FULL_TOKEN_RE = re.compile(r"^LCT1-\d+-(\d+)-([0-9a-f]{12})-\S+$")
CLAIM_ID_RE = re.compile(rf"^({STAMP})-([A-Za-z0-9][A-Za-z0-9._:-]*)$")
LANE_REF_RE = re.compile(r"^\s*lane=([^\s|;,()]+)")
LINE_PTR_RE = re.compile(r"(?:ROLLING_WORK_LEDGER\.md|ledger):(\d+)$")
OMN_RE = re.compile(r"OMN-\d+")


@dataclass
class Rec:
    row: Row
    seq: int
    source: str
    lineno: int
    live: bool
    rtype: str
    raw: str
    lane: str = ""
    tickets: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.lane = self.row.token("lane") or ""
        self.tickets = set(
            OMN_RE.findall(
                " ".join(self.row.value(k) or "" for k in ("ticket", "tickets"))
            )
        )


def split_parts(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in re.split(r"[,\s]+", value) if p.strip()]


class Resolver:
    """Binds a close reference to the CLAIM rows it names."""

    def __init__(self, claims: list[Rec]) -> None:
        self.claims = claims
        self.by_stamp: dict[str, list[Rec]] = {}
        self.by_lane: dict[str, list[Rec]] = {}
        self.by_line: dict[int, Rec] = {}
        self.by_digest: dict[str, list[Rec]] = {}
        for c in claims:
            self.by_stamp.setdefault(c.row.stamp, []).append(c)
            if c.lane:
                self.by_lane.setdefault(c.lane, []).append(c)
            if c.live:
                self.by_line[c.lineno] = c
            self.by_digest.setdefault(claim_row_digest(c.raw), []).append(c)
        self.notes: list[str] = []

    @staticmethod
    def related_tiers(closer: Rec) -> list[tuple[str, Callable[[Rec], bool]]]:
        claim_lane = closer.row.token("claim-lane") or ""
        m = re.match(r"^(.+)-r\d*$", closer.lane) if closer.rtype == "CLAIM" else None
        tiers: list[tuple[str, Callable[[Rec], bool]]] = []
        if claim_lane:
            tiers.append(("claim-lane", lambda c: c.lane == claim_lane))
        if closer.lane:
            tiers.append(("lane", lambda c: c.lane == closer.lane))
        if m:
            base = m.group(1)
            tiers.append(("relaunch", lambda c: c.lane == base))
        if closer.tickets:
            tiers.append(("ticket", lambda c: bool(c.tickets & closer.tickets)))
        return tiers

    def by_bare_stamp(self, stamp: str, closer: Rec, ref: str) -> list[Rec]:
        cands = [c for c in self.by_stamp.get(stamp, []) if c.seq < closer.seq]
        for name, pred in self.related_tiers(closer):
            sel = [c for c in cands if pred(c)]
            if not sel:
                continue
            if name == "ticket" and len(sel) > 1:
                self.ambiguous(ref, closer, sel)
                return []
            return sel
        if len(cands) > 1:
            self.ambiguous(ref, closer, cands)
            return []
        return cands

    def ambiguous(self, ref: str, closer: Rec, cands: list[Rec]) -> None:
        self.notes.append(
            f"AMBIGUOUS ref={ref} at {closer.source} matches {len(cands)} claims "
            f"(lanes {','.join(sorted(c.lane for c in cands))}); none bound: read those rows"
        )

    def names_any(self, value: str) -> bool:
        return bool(
            TOKEN_RE.search(value)
            or LANE_REF_RE.match(value)
            or STAMP_SEARCH.search(value)
            or any(LINE_PTR_RE.search(p) for p in split_parts(value))
        )

    def resolve(self, value: str, closer: Rec) -> list[Rec]:
        bound: list[Rec] = []
        rest = value
        tokens = list(TOKEN_RE.finditer(value))
        if tokens:
            for t in tokens:
                hits = [
                    c for c in self.by_digest.get(t.group(3), []) if c.seq < closer.seq
                ]
                exact = [c for c in hits if c.live and c.lineno == int(t.group(2))]
                pick = exact or hits[-1:]
                if not pick:
                    self.notes.append(
                        f"UNRESOLVED token {t.group(0)} at {closer.source}: no CLAIM row "
                        "with a lane= field in the files read has its digest"
                    )
                bound.extend(pick)
            rest = TOKEN_RE.sub(" ", value)
        lane_ref = LANE_REF_RE.match(rest)
        if lane_ref:
            lane = lane_ref.group(1)
            cands = [c for c in self.by_lane.get(lane, []) if c.seq < closer.seq]
            hinted = set(STAMP_SEARCH.findall(rest))
            exact = [c for c in cands if c.row.stamp in hinted]
            return bound + (exact or cands[-1:])
        for part in split_parts(rest):
            cid = CLAIM_ID_RE.match(part)
            if cid:
                bound.extend(
                    c
                    for c in self.by_stamp.get(cid.group(1), [])
                    if c.lane == cid.group(2) and c.seq < closer.seq
                )
                continue
            ptr = LINE_PTR_RE.search(part)
            if ptr:
                hit = self.by_line.get(int(ptr.group(1)))
                if (
                    hit is not None
                    and hit.seq < closer.seq
                    and any(p(hit) for _, p in self.related_tiers(closer))
                ):
                    bound.append(hit)
                continue
            if STAMP_RE.match(part):
                bound.extend(self.by_bare_stamp(part, closer, part))
        return bound


@dataclass
class Ledger:
    """The parsed rows of the archive rolls and the live ledger, in append order."""

    files: list[Path]
    recs: list[Rec]
    skipped: int

    @property
    def claims(self) -> list[Rec]:
        return [r for r in self.recs if r.rtype == "CLAIM" and r.lane]


def read_ledger(ledger: Path) -> Ledger:
    files: list[tuple[Path, bool]] = []
    archive = ledger.parent / "archive"
    if archive.is_dir():
        files.extend((p, False) for p in sorted(archive.glob(ARCHIVE_GLOB)))
    files.append((ledger, True))
    recs: list[Rec] = []
    skipped = 0
    seq = 0
    for path, live in files:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for lineno, line in enumerate(handle, 1):
                row = parse_row(line)
                if row is None:
                    if opens_a_row(line):
                        skipped += 1
                    continue
                seq += 1
                label = "live" if live else f"archive/{path.name}"
                recs.append(
                    Rec(
                        row=row,
                        seq=seq,
                        source=f"{label}:{lineno}",
                        lineno=lineno,
                        live=live,
                        rtype=LEGACY_TYPE_MAP.get(row.row_type, row.row_type),
                        raw=line.rstrip("\n"),
                    )
                )
    return Ledger([p for p, _ in files], recs, skipped)


def closed_claims(ledger: Ledger, resolver: Resolver) -> set[int]:
    closed: set[int] = set()
    for r in ledger.recs:
        if r.rtype == "TERMINAL":
            value = r.row.value("closes-claim") or ""
            if value and resolver.names_any(value):
                closed.update(c.seq for c in resolver.resolve(value, r))
            else:
                # A closer carrying claim-lane= acts for that lane; without it, its lane= is the subject.
                lanes = {r.row.token("claim-lane") or ""} - {""} or {r.lane} - {""}
                for lane in lanes:
                    closed.update(
                        c.seq for c in resolver.by_lane.get(lane, []) if c.seq < r.seq
                    )
        elif r.rtype == "CLAIM":
            value = r.row.value("supersedes-claim") or ""
            if value:
                closed.update(c.seq for c in resolver.resolve(value, r))
        elif r.rtype == "RELEASE":
            value = r.row.token("re") or ""
            if value:
                closed.update(c.seq for c in resolver.resolve(value, r))
    return closed


class Ownership:
    """Decides whose a CLAIM is under one session scope (see the module docstring)."""

    def __init__(self, scope: ModelSessionScope, claims: Iterable[Rec]) -> None:
        self.scope = scope
        principals_by_lane: dict[str, list[set[str]]] = {}
        for c in claims:
            principals_by_lane.setdefault(c.lane, []).append(c.row.principals())
        # A lane is the member's own only when every CLAIM of it carries only the member's principals.
        self.own_lanes = {
            lane
            for lane, sets in principals_by_lane.items()
            if all(s and s <= set(scope.principals) for s in sets)
        }

    def owner(self, claim: Rec) -> Owner:
        if self.scope.is_operator:
            return "own"
        principals = claim.row.principals()
        if principals:
            return "own" if principals <= set(self.scope.principals) else "foreign"
        parent = claim.row.token("parent")
        return "own" if parent and parent in self.own_lanes else "foreign"


def find_claims(ledger: Ledger, dead_claim: str, dead_lane: str) -> list[Rec]:
    """The CLAIM rows a stopped-claim reference (stamp or LCT1 token) names for ``dead_lane``."""
    token = FULL_TOKEN_RE.match(dead_claim)
    claims = [c for c in ledger.claims if c.lane == dead_lane]
    if token:
        return [c for c in claims if claim_row_digest(c.raw) == token.group(2)]
    return [c for c in claims if c.row.stamp == dead_claim]


def dangling(
    ledger: Ledger,
    scope: ModelSessionScope,
    *,
    since: str,
    t0: str | None,
) -> tuple[list[ModelDanglingClaim], list[str], str]:
    all_claims = ledger.claims
    resolver = Resolver(all_claims)
    closed = closed_claims(ledger, resolver)
    ownership = Ownership(scope, all_claims)
    last_row: dict[str, str] = {}
    for r in ledger.recs:
        for key in ("lane", "from"):
            who = r.row.token(key)
            if who and r.row.stamp > last_row.get(who, ""):
                last_row[who] = r.row.stamp
    candidates = [c for c in all_claims if c.row.stamp >= since]
    out: list[ModelDanglingClaim] = []
    for c in candidates:
        if c.seq in closed:
            continue
        last = last_row.get(c.lane, c.row.stamp)
        state = "UNKNOWN" if not t0 else ("ACTIVE" if last >= t0 else "QUIET")
        out.append(
            ModelDanglingClaim(
                claim_stamp=c.row.stamp,
                lane=c.lane,
                state=state,
                last_row=last,
                source=c.source,
                ticket=c.row.value("ticket") or c.row.value("tickets") or "",
                repo=c.row.value("repo") or "",
                pr=c.row.value("pr") or "",
                worktree=c.row.value("worktree") or "",
                parent=c.row.token("parent") or "",
                consent=c.row.token("consent") or "",
                principal=",".join(sorted(c.row.principals())),
                owner=ownership.owner(c),
            )
        )
    foreign = sum(1 for item in out if item.owner == "foreign")
    summary = (
        f"# files={len(ledger.files)} parsed_rows={len(ledger.recs)} "
        f"non_canonical_skipped={ledger.skipped} claims_since={since} "
        f"candidates={len(candidates)} dangling={len(out)} foreign={foreign} "
        f"ref_notes={len(resolver.notes)}"
    )
    return out, resolver.notes, summary


def dangling_prs(items: list[ModelDanglingClaim]) -> tuple[list[str], list[str]]:
    """The ``repo#n`` PRs the dangling CLAIMs name, and an AMBIGUOUS note per unjoinable number."""
    prs: list[str] = []
    notes: list[str] = []
    for item in items:
        repos = [r.split("/")[-1] for r in split_parts(item.repo)]
        for part in split_parts(item.pr):
            part = part.split("/")[-1]
            if "#" in part:
                ref = part
            elif part.isdigit() and len(repos) == 1:
                ref = f"{repos[0]}#{part}"
            else:
                notes.append(
                    f"AMBIGUOUS pr={part} repo={item.repo or '-'} lane={item.lane}"
                )
                continue
            if ref not in prs:
                prs.append(ref)
    return prs, notes
