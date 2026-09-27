# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reconcile the board against its projection — read, derive, report. Never write.

This is the surface that replaces the manual board sweep. It reads the rolling
work ledger, merges those claims into the fact bundles, runs the pure projection
(OMN-16731), and renders the diff table a person used to produce by hand.

It has no write path. That is not a default someone can flip — the code to
mutate Linear does not exist in this node. Arming live writes is OMN-16733, and
it lands with its own preconditions: the In-Review fabrication fixed (OMN-16536),
the Done edge owned (OMN-16106), and a reviewed dry-run.

On the ledger grammar: the rolling ledger is markdown written by many sessions,
and its CLAIM/TERMINAL rows are genuinely heterogeneous — a leading pipe is
optional, the ticket field sometimes precedes the verb and sometimes appears
only in the prose body, and separators are mixed. The parser handles what is
actually there rather than what a schema would prefer, and a row it cannot bind
to a ticket is dropped rather than guessed at. That messiness is itself the
argument for this projection: the ledger is the pragmatic MVP of an event log,
and parsing it is a migration step, not the end state.

Related:
    - OMN-16729: Epic — board-truth mechanization
    - OMN-16732: this node
    - OMN-16730: the fact-source adapters this will consume once they land
    - OMN-16733: the live writer, deliberately absent here
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Literal

from omnimarket.enums.enum_board_reconcile_action import EnumBoardReconcileAction
from omnimarket.events.board_truth import (
    ModelBoardFactBundle,
    ModelBoardTruthInput,
    ModelBoardTruthOutput,
    ModelLedgerClaimFact,
)
from omnimarket.nodes.node_board_truth_compute.handlers.handler_board_truth import (
    HandlerBoardTruth,
)
from omnimarket.nodes.node_board_truth_reconcile_effect.models.model_board_reconcile_input import (
    ModelBoardReconcileInput,
)
from omnimarket.nodes.node_board_truth_reconcile_effect.models.model_board_reconcile_output import (
    ModelBoardReconcileOutput,
)
from omnimarket.nodes.node_board_truth_reconcile_effect.models.model_board_reconcile_summary import (
    ModelBoardReconcileSummary,
)

logger = logging.getLogger(__name__)

_TICKET_RE = re.compile(r"\bOMN-\d+\b")
_VERB_RE = re.compile(r"^(CLAIM|TERMINAL|PARKED)$", re.IGNORECASE)
_TS_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}:\d{2}))?Z?\b")

# Verbs that end a claim. PARKED is included because a parked lane is exactly as
# dead as a terminated one from the board's point of view.
_TERMINAL_VERBS: frozenset[str] = frozenset({"TERMINAL", "PARKED"})


def parse_ledger_claims(text: str) -> dict[str, tuple[ModelLedgerClaimFact, ...]]:
    """Extract CLAIM/TERMINAL rows from the rolling ledger, keyed by ticket.

    A row binds to whichever tickets are cited in the pipe-delimited fields
    *before* the verb. When no ticket precedes the verb — several real rows put
    them only in the prose body — the body's ticket citations are used instead.
    A row with no ticket anywhere is dropped, never attributed by proximity.

    CLAIM and TERMINAL rows are **paired by (ticket, lane)**: a TERMINAL closes
    the most recent still-open claim from the same lane rather than becoming a
    separate fact. Without that pairing a lane that opened and closed a claim
    would leave the open half behind forever, and every abandoned lane would read
    as live work — which would defeat the reaper edge entirely.

    A TERMINAL with no matching open claim is recorded as an already-closed fact.
    That happens legitimately: a lane's CLAIM may predate the retained window, or
    live in a row shape this parser drops.

    Args:
        text: Raw ledger markdown.

    Returns:
        Ticket identifier mapped to the claim facts citing it, in file order.
    """
    claims: dict[str, list[ModelLedgerClaimFact]] = {}
    # (ticket, lane) -> index of that lane's newest still-open claim.
    open_claims: dict[tuple[str, str], int] = {}

    for line in text.splitlines():
        if "|" not in line:
            continue
        segments = [segment.strip() for segment in line.split("|")]
        verb_index = next(
            (i for i, segment in enumerate(segments) if _VERB_RE.match(segment)), None
        )
        if verb_index is None:
            continue

        verb = segments[verb_index].upper()
        before = segments[:verb_index]
        after = segments[verb_index + 1 :]

        tickets = _tickets_in(before) or _tickets_in(after)
        if not tickets:
            continue

        timestamp = _timestamp_in(before) or _timestamp_in(after)
        if timestamp is None:
            continue

        lane = _lane_in(before)

        for ticket in tickets:
            rows = claims.setdefault(ticket, [])
            key = (ticket, lane)

            if verb not in _TERMINAL_VERBS:
                open_claims[key] = len(rows)
                rows.append(
                    ModelLedgerClaimFact(
                        lane=lane, claimed_at=timestamp, terminal_at=None
                    )
                )
                continue

            index = open_claims.pop(key, None)
            if index is None:
                rows.append(
                    ModelLedgerClaimFact(
                        lane=lane, claimed_at=timestamp, terminal_at=timestamp
                    )
                )
            else:
                rows[index] = rows[index].model_copy(update={"terminal_at": timestamp})

    return {ticket: tuple(facts) for ticket, facts in claims.items()}


def _tickets_in(segments: list[str]) -> list[str]:
    """Ticket ids in order of first appearance, de-duplicated."""
    seen: list[str] = []
    for segment in segments:
        for match in _TICKET_RE.findall(segment):
            if match not in seen:
                seen.append(match)
    return seen


def _timestamp_in(segments: list[str]) -> datetime | None:
    for segment in segments:
        match = _TS_RE.search(segment)
        if match is None:
            continue
        date_part, time_part = match.group(1), match.group(2) or "00:00:00"
        try:
            return datetime.fromisoformat(f"{date_part}T{time_part}").replace(
                tzinfo=UTC
            )
        except ValueError:  # pragma: no cover - regex already constrains the shape
            continue
    return None


def _lane_in(segments: list[str]) -> str:
    """The lane handle: the last pre-verb field that is neither a timestamp nor tickets."""
    for segment in reversed(segments):
        if not segment:
            continue
        if _TS_RE.fullmatch(segment.strip()) or _TS_RE.match(segment.strip()):
            continue
        if _TICKET_RE.search(segment) and not _TICKET_RE.sub("", segment).strip(" ,/"):
            continue
        return segment
    return "unattributed"


class HandlerBoardTruthReconcile:
    """Dry-run reconciliation: read facts, derive truth, render the diff table."""

    node_type: Literal["effect"] = "effect"

    def handle(self, request: ModelBoardReconcileInput) -> ModelBoardReconcileOutput:
        """Produce the projection and its rendered diff table.

        Args:
            request: Board fact bundles, the ledger to merge, and the evaluation
                instant and staleness window.

        Returns:
            The projection, the rendered report, and headline counts. Nothing is
            written anywhere.
        """
        ledger_claims = self._read_ledger(request)
        bundles = tuple(self._merge_claims(b, ledger_claims) for b in request.tickets)

        projection = HandlerBoardTruth().handle(
            ModelBoardTruthInput(
                correlation_id=request.correlation_id,
                evaluated_at=request.evaluated_at,
                staleness_days=request.staleness_days,
                tickets=bundles,
            )
        )

        summary = ModelBoardReconcileSummary(
            evaluated_count=len(projection.rows),
            no_change_count=sum(
                1
                for r in projection.rows
                if r.action is EnumBoardReconcileAction.NO_CHANGE
            ),
            flip_count=sum(
                1 for r in projection.rows if r.action is EnumBoardReconcileAction.FLIP
            ),
            discrepancy_count=sum(
                1
                for r in projection.rows
                if r.action is EnumBoardReconcileAction.DISCREPANCY
            ),
            requires_confirmation_count=sum(
                1 for r in projection.rows if r.requires_human_confirmation
            ),
            ledger_claims_parsed=sum(len(v) for v in ledger_claims.values()),
        )

        return ModelBoardReconcileOutput(
            projection=projection,
            report=self._render(projection, summary, request.evaluated_at),
            summary=summary,
        )

    # -- effect boundary ----------------------------------------------------

    def _read_ledger(
        self, request: ModelBoardReconcileInput
    ) -> dict[str, tuple[ModelLedgerClaimFact, ...]]:
        """The only I/O in this node: one file read."""
        if request.ledger_path is None:
            return {}
        text = request.ledger_path.read_text(encoding="utf-8")
        claims = parse_ledger_claims(text)
        logger.debug("parsed ledger claims for %d tickets", len(claims))
        return claims

    def _merge_claims(
        self,
        bundle: ModelBoardFactBundle,
        ledger_claims: dict[str, tuple[ModelLedgerClaimFact, ...]],
    ) -> ModelBoardFactBundle:
        parsed = ledger_claims.get(bundle.ticket, ())
        if not parsed:
            return bundle
        return bundle.model_copy(
            update={"ledger_claims": tuple(bundle.ledger_claims) + parsed}
        )

    # -- rendering ----------------------------------------------------------

    def _render(
        self,
        projection: ModelBoardTruthOutput,
        summary: ModelBoardReconcileSummary,
        evaluated_at: datetime,
    ) -> str:
        flips = [
            r for r in projection.rows if r.action is EnumBoardReconcileAction.FLIP
        ]
        discrepancies = [
            r
            for r in projection.rows
            if r.action is EnumBoardReconcileAction.DISCREPANCY
        ]

        lines: list[str] = [
            f"BOARD-TRUTH DRY RUN — evaluated at {evaluated_at.isoformat()}",
            "no writes performed; the live writer is OMN-16733 and is not armed",
            "",
            f"evaluated {summary.evaluated_count} · "
            f"{summary.no_change_count} already correct · "
            f"{summary.flip_count} would flip · "
            f"{summary.discrepancy_count} discrepancies · "
            f"{summary.requires_confirmation_count} need confirmation before any write",
            "",
        ]

        if flips:
            lines.append("FLIPS")
            for row in flips:
                confirm = (
                    " [needs confirmation]" if row.requires_human_confirmation else ""
                )
                lines.append(
                    f"  {row.ticket:<12} {row.current_state:<14} -> "
                    f"{row.derived_state.value:<12} FLIP{confirm}"
                )
                for item in row.evidence:
                    lines.append(f"      · {item}")
        else:
            lines.append("FLIPS — none; the board agrees with its facts")

        lines.append("")
        if discrepancies:
            lines.append("DISCREPANCIES (reported, never acted on)")
            for row in discrepancies:
                lines.append(
                    f"  {row.ticket:<12} {row.current_state:<14} -> "
                    f"{row.derived_state.value:<12} {row.discrepancy_reason or ''}"
                )
                for item in row.evidence:
                    lines.append(f"      · {item}")
        else:
            lines.append("DISCREPANCIES — none")

        return "\n".join(lines)


__all__: list[str] = ["HandlerBoardTruthReconcile", "parse_ledger_claims"]
