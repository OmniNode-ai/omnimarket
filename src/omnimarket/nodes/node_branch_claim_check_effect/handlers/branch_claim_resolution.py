# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure port of claim_index replay and branch_claim resolution.

Database ordering and row citations replace file ordering and line numbers.
The ledger witness never participates in the holder decision.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_branch_claim_check_effect.models import (
    EnumBranchClaimOutcome as Outcome,
)
from omnimarket.nodes.node_branch_claim_check_effect.models import (
    ModelBranchClaimPolicy,
)

WorkLedgerRow = tuple[str, datetime, str]
_STAMP_RE = re.compile(r"^\s*-?\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z)")
_LANE_RE = re.compile(r"^\s*lane\s*=\s*([A-Za-z0-9][A-Za-z0-9_-]*)\s*$")
_SUBJECT_FIELD_RE = re.compile(r"^\s*tickets?\s*=", re.IGNORECASE)
_TICKET_RE = re.compile(r"\bOMN-\d+\b")
_TO_RE = re.compile(r"^\s*to\s*=\s*([A-Za-z0-9][A-Za-z0-9_-]*)\s*$")
_STALE_CITATION_RE = re.compile(r"^\s*stale\s*=\s*(\S+):(\d+)\s*$")
_BRANCH_TICKET_RE = re.compile(r"(?i)\bomn-(\d+)\b")
_TRAILER_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):[ \t]*(.*)$")
_IDENTITY_LANE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FENCE_TRAILER = "Onex-Fence"


@dataclass(frozen=True)
class Holder:
    lane: str
    fence: int
    claim_line: str
    claimed_at: str
    last_activity_at: str
    state: str = "held"
    source: str = "omninode_internal.work_ledger_rows"

    def citation(self, short_length: int) -> str:
        return f"work_ledger_rows:{self.claim_line[:short_length]}"


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    ticket: str | None
    holder: Holder | None = None
    lanes: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()


def _fields(row: str) -> list[str]:
    # The file replay acts only on the stamped line. Continuations are part
    # of the raw row's identity, but cannot declare claim metadata.
    lines = row.splitlines()
    return lines[0].split("|") if lines else []


def _stamp(row: str) -> str | None:
    match = _STAMP_RE.match(row)
    return match.group(1) if match else None


def _row_class(row: str) -> str | None:
    fields = _fields(row)
    if _stamp(row) is None or len(fields) < 2:
        return None
    candidate = fields[1].strip().upper()
    return candidate if candidate.isalpha() or "-" in candidate else None


def _field_value(row: str, pattern: re.Pattern[str]) -> str | None:
    for cell in _fields(row):
        match = pattern.match(cell)
        if match:
            return match.group(1)
    return None


def _lane(row: str) -> str | None:
    return _field_value(row, _LANE_RE)


def _subjects(row: str) -> frozenset[str]:
    return frozenset(
        ticket
        for cell in _fields(row)
        if _SUBJECT_FIELD_RE.match(cell)
        for ticket in _TICKET_RE.findall(cell)
    )


def ticket_from_branch(branch: str) -> str | None:
    match = _BRANCH_TICKET_RE.search(branch)
    return f"OMN-{match.group(1)}" if match else None


def _is_stale(held: Holder, now: datetime, hours: int) -> bool:
    last = datetime.fromisoformat(held.last_activity_at).astimezone(UTC)
    return now - last > timedelta(hours=hours)


# The file replay applies rows in append order; the database keeps no append
# order, and row_ts is only second-grained. Lanes write a CLAIM and its
# TERMINAL inside one second, and ordering that pair by row_id (a content hash)
# would apply the TERMINAL first and leave a phantom holder for the staleness
# window. Inside one second a claim opens before activity, and activity comes
# before the release that closes it.
_OPENING_ROWS = frozenset({"CLAIM", "RECLAIM", "HANDOVER"})
_CLOSING_ROWS = frozenset({"RELEASE", "TERMINAL"})


def _causal_rank(raw: str) -> int:
    row_class = _row_class(raw)
    if row_class in _OPENING_ROWS:
        return 0
    return 2 if row_class in _CLOSING_ROWS else 1


def build_index(
    rows: Sequence[WorkLedgerRow], *, now: datetime, staleness_hours: int
) -> dict[str, Holder]:
    """Replay the database window in (row_ts, causal rank, row_id) order."""
    tickets: dict[str, Holder] = {}
    released_fences: dict[str, int] = {}
    ordered = sorted(rows, key=lambda row: (row[1], _causal_rank(row[2]), row[0]))
    for row_id, _, raw in ordered:
        row_class, lane, stamp = _row_class(raw), _lane(raw), _stamp(raw)
        if row_class is None or lane is None or stamp is None:
            continue
        for ticket in _subjects(raw):
            current = tickets.get(ticket)
            live = current is not None and not _is_stale(
                current, datetime.fromisoformat(stamp), staleness_hours
            )
            if row_class == "CLAIM":
                if live and current is not None:
                    if current.lane == lane:
                        tickets[ticket] = replace(current, last_activity_at=stamp)
                    continue
                fence = (
                    current.fence if current else released_fences.get(ticket, 0)
                ) + 1
                tickets[ticket] = Holder(lane, fence, row_id, stamp, stamp)
                continue
            if current is None:
                continue
            if row_class in {"RELEASE", "TERMINAL"}:
                if current.lane == lane:
                    released_fences[ticket] = current.fence
                    del tickets[ticket]
                continue
            if row_class == "HANDOVER":
                receiver = _field_value(raw, _TO_RE)
                if current.lane == lane and receiver:
                    tickets[ticket] = Holder(
                        receiver, current.fence + 1, row_id, stamp, stamp
                    )
                continue
            if row_class == "RECLAIM":
                cites_stale = any(
                    _STALE_CITATION_RE.match(cell) for cell in _fields(raw)
                )
                if not live and cites_stale:
                    tickets[ticket] = Holder(
                        lane, current.fence + 1, row_id, stamp, stamp
                    )
                elif current.lane == lane:
                    tickets[ticket] = replace(current, last_activity_at=stamp)
                continue
            if current.lane == lane:
                tickets[ticket] = replace(current, last_activity_at=stamp)
    return {
        ticket: replace(
            held, state="stale" if _is_stale(held, now, staleness_hours) else "held"
        )
        for ticket, held in tickets.items()
    }


def _split_comments(message: str) -> tuple[str, str]:
    lines = message.splitlines(keepends=True)
    first_comment = len(lines)
    for index in range(len(lines) - 1, -1, -1):
        stripped = lines[index].strip()
        if stripped.startswith("#") or not stripped:
            first_comment = index
        else:
            break
    return "".join(lines[:first_comment]), "".join(lines[first_comment:])


def commit_trailers(message: str) -> dict[str, str]:
    body, _ = _split_comments(message)
    trailers: dict[str, str] = {}
    for line in reversed([line for line in body.splitlines() if line.strip()]):
        match = _TRAILER_LINE_RE.match(line)
        if match is None:
            break
        trailers.setdefault(match.group(1), match.group(2).strip())
    return trailers


def valid_lane(lane: str, max_length: int) -> bool:
    return len(lane) <= max_length and _IDENTITY_LANE_RE.fullmatch(lane) is not None


def commit_identity(message: str, max_lane_length: int) -> tuple[str, str] | None:
    trailers = commit_trailers(message)
    lane, session = trailers.get("Onex-Lane"), trailers.get("Onex-Session")
    if not lane or not session or not valid_lane(lane, max_lane_length):
        return None
    return lane, session


def _fence(message: str) -> int | None:
    raw = commit_trailers(message).get(FENCE_TRAILER)
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def refusal_for_push(
    held: Holder | None,
    ticket: str,
    lane: str,
    *,
    fence: int | None,
    policy: ModelBranchClaimPolicy,
) -> str | None:
    if held is None or held.state == "stale":
        return None
    citation = held.citation(policy.short_sha_length)
    release = (
        "Release path -- the holder releases, then the new lane claims:\n"
        f"    <ts> | RELEASE | lane={held.lane} | re={held.claimed_at} | ticket={ticket} | <why it is being given up>\n"
        f"    <ts> | CLAIM | lane=<your lane> | ticket={ticket} | <scope and cost sentence>\n"
        f"and if that lane is gone, wait for the claim to go stale ({policy.staleness_hours}h with no row from it on this ticket) and then:\n"
        f"    <ts> | CLAIM | lane=<your lane> | ticket={ticket} | supersedes-claim={held.claimed_at} | last_activity={held.last_activity_at} | <why>"
    )
    if held.lane != lane:
        return (
            f"push to {ticket} refused: it is held by {held.lane} (claim {citation}, taken {held.claimed_at}, last active {held.last_activity_at}, fence {held.fence}), and this lane is {lane}.\n"
            + release
        )
    if fence is not None and fence < held.fence:
        return (
            f"push to {ticket} refused: these commits carry fence {fence}, but the live claim is at fence {held.fence} -- the claim was taken over while this lane kept working ({citation}). Re-read the ledger before continuing.\n"
            + release
        )
    return None


def resolve_with_index(
    index: dict[str, Holder],
    *,
    branch: str,
    commits: Sequence[tuple[str, str]],
    policy: ModelBranchClaimPolicy,
) -> Verdict:
    ticket = ticket_from_branch(branch)
    if ticket is None:
        return Verdict(Outcome.NO_TICKET, None)
    held = index.get(ticket)
    live_holder = held if held is not None and held.state == "held" else None
    findings: list[str] = []
    outcomes: set[Outcome] = set()
    lanes: list[str] = []
    for sha, message in commits:
        identity = commit_identity(message, policy.max_lane_length)
        if identity is None:
            outcomes.add(Outcome.UNIDENTIFIED)
            findings.append(
                f"{sha[: policy.short_sha_length]} carries no resolvable lane identity, so this push cannot be compared against the claim on {ticket}. An absent identifier and a wrong one are indistinguishable downstream, which is why this reports rather than passing.\n    Register the worktree once, then amend or re-commit:\n      python3 scripts/lane_identity.py register --lane <slug> --ticket {ticket}"
            )
            continue
        lane = identity[0]
        if lane not in lanes:
            lanes.append(lane)
        reason = refusal_for_push(
            held, ticket, lane, fence=_fence(message), policy=policy
        )
        if reason is None:
            outcomes.add(Outcome.HELD_BY_PUSHER if live_holder else Outcome.UNCLAIMED)
        else:
            outcomes.add(
                Outcome.HELD_ELSEWHERE
                if held is not None and held.lane != lane
                else Outcome.FENCE_BEHIND
            )
            findings.append(f"{sha[: policy.short_sha_length]}: {reason}")
    if not commits:
        outcomes.add(Outcome.HELD_BY_PUSHER if live_holder else Outcome.UNCLAIMED)
    outcome = next(kind for kind in Outcome if kind in outcomes)
    return Verdict(outcome, ticket, live_holder, tuple(lanes), tuple(findings))
