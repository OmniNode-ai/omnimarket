# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Derive the triage fence list from ledger text and open pull requests (OMN-17757, OMN-20682).

Why this exists: on 2026-09-03 a fan-out triage lane derived its fenced ticket list by hand and
missed a dangling CLAIM whose lane never went TERMINAL, so a live-claimed ticket was adjudicated
anyway. A list assembled by eye is not reproducible and cannot be regression-tested. The handler
reads no file and no clock: the same ledger text and the same ``now`` give a byte-identical list.

The grammar is in ``logic.triage_fences_grammar``, the CLAIM / TERMINAL pairing and the lane
liveness window in ``logic.triage_fences_claims``, and the pull-request matching in
``logic.triage_fences_pulls``.
"""

from __future__ import annotations

from ..logic.triage_fences_claims import (
    claim_model,
    lane_last_seen,
    open_claims,
    split_live_and_stale,
)
from ..logic.triage_fences_grammar import parse_ledger_text, sort_tickets
from ..logic.triage_fences_pulls import pr_fences_from_pulls
from ..models import (
    ModelTriageFencesCounts,
    ModelTriageFencesDeriveRequest,
    ModelTriageFencesVerdict,
)


class HandlerTriageFencesDerive:
    """Same ledger text and the same clock give the same fence list, byte for byte."""

    def handle(
        self, request: ModelTriageFencesDeriveRequest
    ) -> ModelTriageFencesVerdict:
        """Return the fence list for one ledger."""
        now = request.now
        parsed = parse_ledger_text(request.ledger_text, request.ledger_name)
        claims, stale = split_live_and_stale(
            open_claims(parsed.rows, now=now),
            last_seen=lane_last_seen(request.ledger_text, parsed.rows),
            now=now,
            stale_after_hours=request.stale_after_hours,
            running_lanes=request.running_lanes,
        )
        pr_fences = pr_fences_from_pulls(
            request.open_pulls, window_hours=request.pr_window_hours, now=now
        )
        fenced = {ticket for claim in claims for ticket in claim.tickets}
        fenced |= {fence.ticket for fence in pr_fences}
        return ModelTriageFencesVerdict(
            fenced_tickets=tuple(sort_tickets(fenced)),
            open_claims=tuple(claim_model(claim) for claim in claims),
            stale_claims=tuple(claim_model(claim) for claim in stale),
            open_pr_fences=pr_fences,
            unparsed_rows=tuple(parsed.unparsed),
            counts=ModelTriageFencesCounts(
                rows_parsed=len(parsed.rows),
                open_claims=len(claims),
                stale_claims=len(stale),
                open_pr_fences=len(pr_fences),
                fenced_tickets=len(fenced),
                unparsed_rows=len(parsed.unparsed),
            ),
        )
