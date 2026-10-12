# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Findings, their row builders and the apply gates of the reconciler (OMN-17466, moved by OMN-20677)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .evidence import (
    COMPLETED,
    UNKNOWN,
    Evidence,
    EvidenceContext,
    extract_evidence,
    parse_iso,
    verdict_for,
)
from .facts import FactBook
from .ledger_row_grammar import parse_row
from .ledger_rows import (
    CLASS_WORD_RE,
    OUT_OF_SCOPE_RE,
    PR_REF_RE,
    TS_LEAD_RE,
    WATCH_STEP_RE,
    Row,
    classify_kind,
    parse_ts,
    row_lane,
)

RECONCILER_TICKET = "OMN-17466"

# Marker literals — the idempotency scan greps for these verbatim.
AUTO_CLOSE_MARK = "RECONCILER-AUTO-CLOSE"
ATTENTION_MARK = "RECONCILER-NEEDS-ATTENTION"
RELEASE_MARK = "RECONCILER-AUTO-RELEASE"
ABANDONED = "ABANDONED"
LANE_ACTIVE_HOURS = 2.0


@dataclass
class Finding:
    claim: Row
    age_hours: float
    verdict: str
    detail: str
    evidence: Evidence
    already_flagged: bool = False
    applied: str = (
        ""  # "", "terminal-appended", "attention-appended", "append-failed: ...",
        # "refused-by-cap"
    )
    lane_active: str = ""  # why an ORPHANED claim's lane reads as still working
    # Why --apply writes nothing for this claim whatever its verdict: the lane
    # is on the live roster, or a COMPLETED scope names a step after landing.
    held: str = ""


# --- Apply ------------------------------------------------------------------


def claim_identity(claim: Row) -> str:
    lane = claim.lane or "/".join(sorted(claim.tickets)) or "unknown-lane"
    return f"[{lane} @ {claim.ts.strftime('%Y-%m-%dT%H:%M:%SZ')}]"


def reconciler_rows(ledger_texts: list[str]) -> list[str]:
    """Every ledger line a reconciler pass wrote: the only lines an identity can be flagged on."""
    return [
        line
        for text in ledger_texts
        for line in text.splitlines()
        if AUTO_CLOSE_MARK in line or ATTENTION_MARK in line
    ]


def is_flagged(claim: Row, rows: list[str]) -> bool:
    """True if a RECONCILER-* row for this exact (lane, claim-ts) identity is among ``rows``."""
    ident = claim_identity(claim)
    return any(ident in line for line in rows)


def already_flagged(claim: Row, ledger_texts: list[str]) -> bool:
    """True if a RECONCILER-* row for this exact (lane, claim-ts) identity is
    already on any ledger surface (live or archive -- a roll moves rows)."""
    return is_flagged(claim, reconciler_rows(ledger_texts))


def open_holds(rows: list[Row]) -> list[Row]:
    """Unreleased canonical HOLDs, paired across rolls by exact id only."""
    released = set()
    for row in rows:
        parsed = parse_row(row.raw)
        if parsed is not None and row.kind == "RELEASE" and parsed.token("re"):
            released.add(parsed.token("re"))
    return [
        row
        for row in rows
        if row.kind == "HOLD"
        and (parsed := parse_row(row.raw)) is not None
        and parsed.token("id") not in released
    ]


def hold_finding(
    hold: Row,
    context: EvidenceContext,
    book: FactBook,
    now: datetime,
    live_lanes: frozenset[str],
    own_rows: dict[str, datetime],
) -> Finding:
    parsed = parse_row(hold.raw)
    assert parsed is not None
    # Only structured PR targets/dependencies count; prose may cite unrelated work.
    handles = []
    invalid = False
    for key in ("pr", "after"):
        for value in parsed.fields.get(key, []):
            for token in value.split(","):
                token = token.strip()
                if token.isdigit() and parsed.token("repo"):
                    token = f"{parsed.token('repo')}#{token}"
                if not PR_REF_RE.fullmatch(token):
                    invalid = True
                handles.append(token)
    evidence = extract_evidence(
        " ".join(handles), context.clones, context.aliases, context.branch_prefixes
    )
    for pr in evidence.prs:
        book.apply_pr(pr)
    verdict, detail = verdict_for(evidence)
    if invalid or not evidence.prs or any(pr.repo is None for pr in evidence.prs):
        verdict, detail = (
            UNKNOWN,
            "missing or unresolvable structured PR release condition",
        )
    if verdict == COMPLETED and any(
        parse_iso(pr.merged_at) is None
        or not re.fullmatch(r"[0-9a-f]{7,40}", pr.merge_sha)
        for pr in evidence.prs
    ):
        verdict, detail = UNKNOWN, "merged PR lacks a verifiable merge timestamp/SHA"
    finding = Finding(
        hold, (now - hold.ts).total_seconds() / 3600, verdict, detail, evidence
    )
    if hold.lane in live_lanes:
        finding.held = "lane is on the --live-lanes roster"
    elif hold.lane in own_rows:
        finding.held = "holder wrote a recent ledger row"
    elif not parsed.token("id") or not hold.lane:
        finding.held = "hold identity is missing"
    elif parsed.value("release") or parsed.value("ruling"):
        finding.held = "operator or other explicit release authority required"
    elif parsed.value("proof") or parsed.value("surface"):
        finding.held = "proof/surface restoration is not established by a PR merge"
    elif parsed.value("until"):
        until = parse_ts(parsed.value("until") or "")
        if until is None or until > now:
            finding.held = "until= is unreadable or has not elapsed"
    return finding


def build_release_row(finding: Finding, now: datetime) -> str:
    parsed = parse_row(finding.claim.raw)
    assert parsed is not None
    assert parsed.token("id")
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"{stamp} | RELEASE | lane=ledger-reconciler | re={parsed.token('id')} | "
        f"ticket={','.join(sorted(finding.claim.tickets)) or RECONCILER_TICKET} | "
        f"actor=script:ledger-reconciler | **{RELEASE_MARK} ({RECONCILER_TICKET})** — "
        f"stale PR HOLD released; every structured PR target/dependency verified: {finding.detail}."
    )


def _grammar_token(value: str | None) -> str:
    """`value` as one whitespace-free token, the shape the ledger row grammar
    requires of lane= (OMN-19256)."""
    return re.sub(r"\s+", "-", (value or "").strip())


def build_terminal_row(finding: Finding, now: datetime) -> str:
    claim = finding.claim
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    lane = _grammar_token(claim.lane) or "reconciler-unlaned"
    tickets = ",".join(sorted(claim.tickets)) or RECONCILER_TICKET
    if finding.verdict == ABANDONED:
        return (
            f"{ts} | TERMINAL | lane={lane} | ticket={tickets} | actor=script:ledger-reconciler | "
            f"outcome=abandoned | friction=stale-lane | "
            f"**{AUTO_CLOSE_MARK} ({RECONCILER_TICKET})** — closes the CLAIM row "
            f"{claim_identity(claim)}; {finding.detail}. No completion or landing is asserted."
        )
    # OMN-19256: a canonical, timestamp-led TERMINAL row. The bar-led shape
    # this used to write is refused by onex-ledger's row grammar. The row
    # closes the claim for its OWN lane, so lane= is the claim's lane. The writer is a
    # script, so the row is exempt from the delegation and worktree cells (OMN-17427, OMN-20148).
    return (
        f"{ts} | TERMINAL | lane={lane} | ticket={tickets} | actor=script:ledger-reconciler | "
        f"friction=none | "
        f"**{AUTO_CLOSE_MARK} ({RECONCILER_TICKET})** — closes the CLAIM row "
        f"{claim_identity(claim)}; the owning lane ended without appending its own TERMINAL "
        f"(claim age {finding.age_hours:.1f}h at reconcile time). Outcome: landed. "
        f"Evidence verified against LIVE state by onex-ledger-reconcile — {finding.detail}. "
        f"Read from gh pr view --json state,mergedAt,mergeCommit / git merge-base against the "
        f"canonical clones, never from a lane self-report."
    )


def build_attention_row(finding: Finding, now: datetime) -> str:
    claim = finding.claim
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    lane = _grammar_token(claim.lane) or "reconciler-unlaned"
    tickets = ",".join(sorted(claim.tickets)) or RECONCILER_TICKET
    # OMN-19256: NEEDS-ATTENTION is not a canonical row type. The flag is a
    # STATUS row in the reconciler's own lane naming the claim's lane; it opens
    # and closes nothing, exactly as NEEDS-ATTENTION did.
    return (
        f"{ts} | STATUS | lane=ledger-reconciler | ticket={tickets} | claim-lane={lane} | "
        f"**{ATTENTION_MARK} ({RECONCILER_TICKET})** — the CLAIM row {claim_identity(claim)} "
        f"is stale (age {finding.age_hours:.1f}h) and its cited evidence is verifiably NOT "
        f"landed: {finding.detail}. The work appears to have died mid-flight. The claim is "
        f"left OPEN for adjudication by the owning lane or the operator — the reconciler "
        f"never fabricates a completion. Verified live by onex-ledger-reconcile."
    )


def recent_own_rows(text: str, since: datetime) -> dict[str, datetime]:
    """lane -> the latest stamp of a row that lane wrote ITSELF at or after
    `since`, any row class. A row naming a lane in its body (`source-lane=`,
    a handoff recipient) is not that lane's own row: the lane is read by
    position and leading field only, exactly as for claims. Rows this script
    appended in a lane's name are not that lane's rows."""
    latest: dict[str, datetime] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if any(
            mark in stripped for mark in (AUTO_CLOSE_MARK, ATTENTION_MARK, RELEASE_MARK)
        ):
            continue  # the reconciler writes in the lane's name; not the lane
        if stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
        elif TS_LEAD_RE.match(stripped):
            cells = [c.strip() for c in stripped.split("|")]
        else:
            continue
        ts = parse_ts(cells[0])
        if ts is None or ts < since:
            continue
        kind_idx = next(
            (
                idx
                for idx in range(1, min(len(cells), 6))
                if classify_kind(cells[idx]) or CLASS_WORD_RE.match(cells[idx])
            ),
            None,
        )
        if kind_idx is None:
            continue
        lane = row_lane(cells, kind_idx)
        if lane and (lane not in latest or ts > latest[lane]):
            latest[lane] = ts
    return latest


def claim_scope_text(claim: Row) -> str:
    """The claim body up to its OUT OF SCOPE list, which names what the lane
    will NOT do and so must not make it look like a watch step."""
    return OUT_OF_SCOPE_RE.split(claim.body, maxsplit=1)[0]


def hold_reason(
    finding: Finding, live_lanes: frozenset[str], lane_latest: dict[str, datetime]
) -> str:
    """Why --apply must leave this claim alone, or ''.

    A lane on the live roster is running whatever its row recency says: a lane
    resumed by message writes nothing until it acts (ledger:5745). And a
    merged PR proves only the landing half of a scope that also watches,
    monitors or waits for a receipt, so such a claim is closed only once its
    lane has written some row after it."""
    lane = finding.claim.lane
    if lane and lane in live_lanes:
        return "lane is on the --live-lanes roster"
    if finding.verdict != COMPLETED:
        return ""
    step = WATCH_STEP_RE.search(claim_scope_text(finding.claim))
    if step is None:
        return ""
    last = lane_latest.get(lane or "")
    if last is not None and last > finding.claim.ts:
        return ""
    return (
        f"scope names a post-landing step ('{step.group(0)}') and the lane wrote "
        "no row after the claim; a merged PR proves only the landing half"
    )


def lane_activity(
    finding: Finding, own_rows: dict[str, datetime], since: datetime, book: FactBook
) -> str:
    """Why an ORPHANED claim's lane still reads as working, or ''."""
    lane = finding.claim.lane
    if lane and lane in own_rows:
        return f"own ledger row at {own_rows[lane].strftime('%Y-%m-%dT%H:%M:%SZ')}"
    for pr in finding.evidence.prs:
        if not pr.bound or pr.state != "OPEN":
            continue
        pushed = book.pushed_at(pr)
        if pushed is not None and pushed >= since:
            return f"push to {pr.repo}#{pr.number} at {pushed.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    return ""
