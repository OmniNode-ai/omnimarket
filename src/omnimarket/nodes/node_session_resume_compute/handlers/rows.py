# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compose the rows session-resume writes on a stopped lane's behalf, under the member restriction.

Read-only: these functions compose one row each and append nothing.

* The closing TERMINAL (step 3): written under the stopped lane, ``lane=<stopped lane>``, with the
  resuming lane as ``closed-by=`` and ``closes-CLAIM=`` the claim's LCT1 token or its claim id
  ``<stamp>-<lane>``, never a bare stamp. Modes: after a handoff MSG, landed, released.
* The relaunch CLAIM template (step 4): ``parent=<resuming lane>``, ``supersedes-claim=<claim ref>``,
  the carried ``consent=``. A stopped landing lane or landing sub-lane is never relaunched; it is
  routed to a notice MSG to the landing lane.

The member restriction runs before either is composed, and before the landing route: a member
session gets no row, and no landing notice, for a CLAIM that is not its own. A member's target must
be found in the ledger; an unfound one is unverified and refused the same way.
"""

from __future__ import annotations

import re

from omnimarket.models.session_scope import ModelSessionScope

from ..models.model_session_resume import ModelCloseSpec, ModelRelaunchSpec
from .dangling import FULL_TOKEN_RE, Ledger, Ownership, find_claims
from .ledger_grammar import STAMP, parse_row

STAMP_RE = re.compile(rf"^{STAMP}$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
PR_RE = re.compile(r"^[A-Za-z0-9_.-]+#\d+$")
LINE_RE = re.compile(r"^[^|\n]+$")
CELL_RE = re.compile(r"^[^|\n]*$")


class RefusalError(Exception):
    """The row is not composed; the message says why."""


def claim_ref(dead_claim: str, dead_lane: str) -> str:
    """The unique reference for the stopped CLAIM: its token, or ``<stamp>-<lane>``."""
    if FULL_TOKEN_RE.match(dead_claim):
        return dead_claim
    return f"{dead_claim}-{dead_lane}"


def require_owned(
    scope: ModelSessionScope, ledger: Ledger, dead_claim: str, dead_lane: str
) -> None:
    """Refuse unless this session may act on the named CLAIM."""
    if not (STAMP_RE.match(dead_claim) or FULL_TOKEN_RE.match(dead_claim)):
        raise RefusalError(
            f"dead_claim must be the CLAIM's full UTC stamp or its LCT1 token, got {dead_claim!r}"
        )
    if scope.is_operator:
        return
    targets = find_claims(ledger, dead_claim, dead_lane)
    if not targets:
        raise RefusalError(
            f"member session: no CLAIM of {dead_lane} at {dead_claim} is in the ledger, so it "
            "cannot be verified as this member's own; nothing is written for it"
        )
    ownership = Ownership(scope, ledger.claims)
    foreign = [t for t in targets if ownership.owner(t) == "foreign"]
    if foreign:
        raise RefusalError(
            f"member session: the CLAIM of {dead_lane} at {dead_claim} belongs to another "
            "principal; a member closes, relaunches or notifies only its own lanes"
        )


def _check_cells(cells: list[str]) -> None:
    for cell in cells:
        if not CELL_RE.match(cell):
            raise RefusalError(f"a cell holds a pipe or a newline: {cell!r}")


def _head(ts: str, spec: ModelCloseSpec, ticket: str, outcome: str) -> list[str]:
    return [
        ts,
        "TERMINAL",
        f"lane={spec.dead_lane}",
        f"closed-by={spec.lane}",
        f"closes-CLAIM={claim_ref(spec.dead_claim, spec.dead_lane)}",
        f"ticket={ticket}",
        f"outcome={outcome}",
        "delegation=na:closes-stopped-lane",
    ]


def close_row(spec: ModelCloseSpec, now: str) -> str:
    if spec.lane == spec.dead_lane:
        raise RefusalError("lane and dead_lane are the same lane")
    if spec.mode == "after_msg":
        row = parse_row((spec.after_msg or "").strip())
        if row is None or row.row_type != "MSG":
            raise RefusalError("after_msg is not a MSG row in the canonical shape")
        got = {
            k: row.token(k)
            for k in ("from", "to", "id", "ticket", "repo", "pr", "head")
        }
        missing = [k for k, v in got.items() if not v]
        if missing:
            raise RefusalError(f"after_msg lacks {', '.join(k + '=' for k in missing)}")
        if got["from"] != spec.lane:
            raise RefusalError(
                f"after_msg is from {got['from']}, not from lane {spec.lane}"
            )
        if not row.value("needs"):
            raise RefusalError("after_msg lacks needs=; it is not a handoff MSG")
        cells = [
            *_head(row.stamp, spec, got["ticket"] or "", "handed-off"),
            f"repo={got['repo']}",
            f"pr={got['pr']}",
            f"head={got['head']}",
            f"handed_to={got['to']}",
            f"msg={got['id']}",
            f"friction={spec.friction}",
            f"session-resume closes the CLAIM of {spec.dead_lane}: its PR was handed to "
            f"{got['to']} by the MSG in msg=",
        ]
    elif spec.mode == "landed":
        bad = []
        if not spec.ticket:
            bad.append("ticket is required when landed")
        pr = (spec.pr or "").split("/")[-1]
        if not PR_RE.match(pr):
            bad.append(f"pr must be <repo>#<n> when landed, got {spec.pr!r}")
        if not SHA_RE.match(spec.squash or ""):
            bad.append(f"squash must be the merge sha read live, got {spec.squash!r}")
        if bad:
            raise RefusalError("; ".join(bad))
        cells = [
            *_head(now, spec, spec.ticket or "", "landed"),
            f"repo={pr.split('#', 1)[0]}",
            f"pr={pr}",
            f"squash={spec.squash}",
            f"friction={spec.friction}",
            f"session-resume closes the CLAIM of {spec.dead_lane}: its PR merged at the sha "
            "in squash=",
        ]
    else:
        bad = []
        if not spec.ticket:
            bad.append("ticket is required when released")
        if not spec.reason or not LINE_RE.match(spec.reason):
            bad.append("reason is required when released: one line with no '|'")
        if bad:
            raise RefusalError("; ".join(bad))
        cells = [
            *_head(now, spec, spec.ticket or "", "released"),
            f"friction={spec.friction}",
            f"session-resume closes the CLAIM of {spec.dead_lane}: its work is released, "
            f"{spec.reason}",
        ]
    _check_cells(cells)
    return " | ".join(cells)


def landing_route(spec: ModelRelaunchSpec) -> str | None:
    """Why the stopped lane belongs to the landing lane, or None."""
    if spec.dead_lane == spec.landing_lane:
        return f"{spec.dead_lane} is the landing lane"
    parent = None if spec.dead_parent in (None, "", "-") else spec.dead_parent
    if parent and parent == spec.landing_lane:
        return f"{spec.dead_lane} is a sub-lane of the landing lane (parent={spec.landing_lane})"
    return None


def relaunch_row(spec: ModelRelaunchSpec) -> str:
    consent = None if spec.consent in (None, "", "-") else spec.consent
    new_lane = spec.new_lane or f"{spec.dead_lane}-r"
    cells = [
        "<ts>",
        "CLAIM",
        f"lane={new_lane}",
        f"actor={spec.actor}",
        f"ticket={spec.ticket}",
        f"parent={spec.dispatcher}",
        f"supersedes-claim={claim_ref(spec.dead_claim, spec.dead_lane)}",
    ]
    for key, value in (("repo", spec.repo), ("pr", spec.pr)):
        if value:
            cells.append(f"{key}={value}")
    cells.append(f"worktree={spec.worktree or 'none'}")
    if consent:
        cells.append(f"consent={consent}")
    cells.append(
        f"scope=relaunch of {spec.dead_lane} stopped at {spec.t0}: {spec.scope}"
    )
    cells.append(
        f"est ~{spec.est_hours} lane-hours; displaces {spec.displaces}; ({spec.ticket})"
    )
    _check_cells(cells)
    return " | ".join(cells)
