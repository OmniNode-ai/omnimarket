# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The facts a decision was given, and a record of the ones it asked for and did not get (OMN-20677)."""

from __future__ import annotations

from datetime import datetime

from omnimarket.models.ledger_reconcile import (
    ModelPrRef,
    ModelReconcileFacts,
    ModelReconcileWanted,
    ModelShaRef,
)

from .evidence import PrHandle, ShaHandle, parse_iso


class FactBook:
    """Looks up verified facts; a lookup that finds none is remembered as wanted."""

    def __init__(self, facts: ModelReconcileFacts) -> None:
        self._prs = {(f.repo, f.number): f for f in facts.prs}
        self._shas = {(f.sha, f.candidates): f for f in facts.shas}
        self._pushes = {(f.repo, f.number): f for f in facts.pushes}
        self._wanted_prs: dict[tuple[str, int], None] = {}
        self._wanted_shas: dict[tuple[str, tuple[str, ...]], None] = {}
        self._wanted_pushes: dict[tuple[str, int], None] = {}

    def apply_pr(self, handle: PrHandle) -> None:
        """Fill ``handle`` from its fact; a handle with no resolvable repo stays UNVERIFIED."""
        if handle.repo is None:
            handle.state = "UNVERIFIED"
            return
        fact = self._prs.get((handle.repo, handle.number))
        if fact is None:
            self._wanted_prs[(handle.repo, handle.number)] = None
            handle.state = "UNVERIFIED"
            return
        handle.state = fact.state
        handle.merged_at = fact.merged_at
        handle.merge_sha = fact.merge_sha
        handle.title = fact.title

    def apply_sha(self, handle: ShaHandle, candidates: tuple[str, ...]) -> None:
        fact = self._shas.get((handle.sha, candidates))
        if fact is None:
            self._wanted_shas[(handle.sha, candidates)] = None
            return
        handle.found_in = fact.found_in
        handle.landed = fact.landed
        handle.committer_ts = (
            parse_iso(fact.committer_at) if fact.committer_at else None
        )
        handle.msg_tickets = frozenset(fact.msg_tickets)

    def pushed_at(self, handle: PrHandle) -> datetime | None:
        """When the PR's head commit was last committed: the push proxy."""
        if handle.repo is None:
            return None
        fact = self._pushes.get((handle.repo, handle.number))
        if fact is None:
            self._wanted_pushes[(handle.repo, handle.number)] = None
            return None
        return parse_iso(fact.committed_at) if fact.committed_at else None

    def wanted(self) -> ModelReconcileWanted:
        return ModelReconcileWanted(
            prs=tuple(ModelPrRef(repo=r, number=n) for r, n in self._wanted_prs),
            shas=tuple(ModelShaRef(sha=s, candidates=c) for s, c in self._wanted_shas),
            pushes=tuple(ModelPrRef(repo=r, number=n) for r, n in self._wanted_pushes),
        )
