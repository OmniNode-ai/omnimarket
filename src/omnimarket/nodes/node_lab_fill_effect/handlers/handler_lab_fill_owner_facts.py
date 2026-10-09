# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Owner facts: read every ownership source once for all selected lanes (OMN-20668).

Reads only, and decides nothing: the plan node's ownership operation turns these
facts into a verdict per lane. One read per source for the whole batch, because the
old per-lane reads cost the dispatch window (the 2026-10-08T0210Z fire placed 1 of
12). A source that fails is carried as its error text; the PR registry is read only
when a lane has a PR, and the watcher only when a lane is an approved kind.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..models import ModelLabFillOwnerFacts, ModelLabFillOwnerFactsRequest
from ..protocols import (
    LabFillClaimFacts,
    LabFillPortError,
    ProtocolLabFillClock,
    ProtocolLabFillOwnerReader,
)
from .helpers_lab_fill_effect import default_clock

APPROVED_KINDS = ("process-fix", "partial-node", "wiring")


def _error(exc: Exception) -> str:
    return f"{type(exc).__name__}:{exc}"[:200]


class HandlerLabFillOwnerFacts:
    """Read the claim store, the PR claim registry and the PR watcher's merged PRs."""

    def __init__(
        self,
        reader: ProtocolLabFillOwnerReader | None = None,
        clock: ProtocolLabFillClock | None = None,
    ) -> None:
        from ..protocols.local_lab_fill_adapters import LocalOwnerReader

        self._reader = reader if reader is not None else LocalOwnerReader()
        self._clock = default_clock(clock)

    def handle(self, request: ModelLabFillOwnerFactsRequest) -> ModelLabFillOwnerFacts:
        now = datetime.fromtimestamp(self._clock.now_epoch_s(), UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        claim_index: dict[str, dict[str, str]] | str
        ledger_claims: tuple[dict[str, str], ...] | str
        staleness = 0.0
        try:
            claims: LabFillClaimFacts = self._reader.claims(
                request.ledger_path, sorted({lane.ticket for lane in request.lanes})
            )
            claim_index = (
                claims.index
                if isinstance(claims.index, str)
                else {t: dict(rec) for t, rec in claims.index.items()}
            )
            ledger_claims = (
                claims.open_claims
                if isinstance(claims.open_claims, str)
                else tuple(dict(c) for c in claims.open_claims)
            )
            staleness = claims.staleness_hours
        except LabFillPortError as exc:
            claim_index = ledger_claims = _error(exc)
        pr_claims: dict[str, str] | str = {}
        if any(lane.pr for lane in request.lanes):
            try:
                pr_claims = dict(self._reader.pr_claims(request.pr_claim_cli))
            except LabFillPortError as exc:
                pr_claims = _error(exc)
        merged: dict[str, tuple[str, ...]] | str = {}
        if any(lane.kind in APPROVED_KINDS for lane in request.lanes):
            try:
                merged = {
                    t: tuple(names)
                    for t, names in self._reader.watcher_merged(
                        request.watcher_state_path
                    ).items()
                }
            except LabFillPortError as exc:
                merged = _error(exc)
        return ModelLabFillOwnerFacts(
            claim_index=claim_index,
            ledger_claims=ledger_claims,
            now=now,
            staleness_hours=staleness,
            pr_claims=pr_claims,
            watcher_merged=merged,
        )
