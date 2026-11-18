# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Match work-ledger rows to lane scope and preserve their provenance."""

from __future__ import annotations

import re
from collections.abc import Iterable

from omnimarket.events.model_ledger_row_event import ModelLedgerRowEventBase
from omnimarket.nodes.node_context_source_ledger_effect.models.model_ledger_history import (
    ModelLedgerHistoryReference,
    ModelLedgerHistoryScope,
)


def _path_patterns(scope: ModelLedgerHistoryScope) -> tuple[re.Pattern[str], ...]:
    repository = scope.repository.rsplit("/", 1)[-1]
    prefix = rf"(?:\./)?(?:{re.escape(repository)}/)?"
    paths = (
        path.strip().removeprefix("./").removeprefix(f"{repository}/")
        for path in scope.touched_paths
    )
    return tuple(
        re.compile(rf"(?<![\w./-]){prefix}{re.escape(path)}(?![\w/#-]|\.\w)")
        for path in paths
        if path
    )


def _pull_request_pattern(value: str) -> re.Pattern[str]:
    short = value.rsplit("/", 1)[-1]
    owner = value.rpartition("/")[0] or "OmniNode-ai"
    return re.compile(
        rf"(?<![\w./#-])(?:{re.escape(owner)}/)?{re.escape(short)}(?![\w/#-]|\.\w)",
        re.IGNORECASE,
    )


def select_lane_history(
    rows: Iterable[tuple[ModelLedgerRowEventBase, str]], scope: ModelLedgerHistoryScope
) -> tuple[ModelLedgerHistoryReference, ...]:
    """Join terminal rows to their earlier claims and match whole scope tokens."""
    ticket = re.compile(rf"(?<![\w-]){re.escape(scope.ticket)}(?![\w-])")
    pull_request = (
        _pull_request_pattern(scope.pull_request) if scope.pull_request else None
    )
    paths = _path_patterns(scope)
    claims: dict[str, list[ModelLedgerRowEventBase]] = {}
    references: list[ModelLedgerHistoryReference] = []
    seen: set[str] = set()
    for row, source_ref in sorted(rows, key=lambda pair: pair[0].row_timestamp):
        if row.row_id in seen or row.row_lane == scope.lane:
            continue
        seen.add(row.row_id)
        lane = row.row_lane or "none"
        kind = row.row_type.value
        cells = {cell.key: cell.value for cell in row.cells}
        related_claim: ModelLedgerRowEventBase | None = None
        if kind == "CLAIM" and row.row_lane:
            claims.setdefault(lane, []).append(row)
        if kind == "TERMINAL" and row.row_lane:
            stamp = cells.get("closes-CLAIM") or cells.get("closes")
            related_claim = next(
                (
                    claim
                    for claim in reversed(claims.get(lane, ()))
                    if (
                        stamp in (claim.row_timestamp, claim.row_id)
                        if stamp
                        else (
                            not row.tickets
                            or bool(set(row.tickets).intersection(claim.tickets))
                        )
                    )
                ),
                None,
            )
        text = row.raw_row
        match_text = text + ("\n" + related_claim.raw_row if related_claim else "")
        pr_matches = pull_request is not None and pull_request.search(match_text)
        ticket_matches = ticket.search(match_text)
        if not (
            pr_matches
            or ticket_matches
            or any(path.search(match_text) for path in paths)
        ):
            continue

        claim_pr = (
            next((cell.value for cell in related_claim.cells if cell.key == "pr"), None)
            if related_claim
            else None
        )
        references.append(
            ModelLedgerHistoryReference(
                source_ref=source_ref,
                text=text,
                lane=lane,
                ticket=",".join(
                    row.tickets or (related_claim.tickets if related_claim else ())
                )
                or (scope.ticket if ticket_matches else "none"),
                pr=cells.get("pr")
                or claim_pr
                or (scope.pull_request if pr_matches else None)
                or "none",
                kind=kind,
                outcome=cells.get("outcome") or "none",
                time=row.row_timestamp,
                blocker_class=cells.get("blocker_class")
                or cells.get("blocker-class")
                or ("unclassified" if cells.get("blocker") else "none"),
            )
        )
    return tuple(references)
