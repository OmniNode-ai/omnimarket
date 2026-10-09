# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verify the pull requests and commits a decision asked for against live state (OMN-20677)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from pathlib import Path

from omnimarket.models.ledger_reconcile import ModelReconcileFacts
from omnimarket.models.ledger_reconcile.model_ledger_reconcile_ops import (
    ModelVerifyEvidenceRequest,
    ModelVerifyEvidenceResult,
)

from ..protocols import (
    ProtocolReconcileGit,
    ProtocolReconcileGitHub,
    ProtocolReconcileHost,
    ReconcilePortError,
)
from ..protocols.local_ledger_reconcile_adapters import (
    GhPullRequests,
    GitCommits,
    LocalReconcileHost,
)

# Lookups run beside each other, this many at a time, so a ledger of thousands of claims
# does not pay a round trip per pull request one after the other.
MAX_CONCURRENT_LOOKUPS = 8


class HandlerVerifyEvidence:
    """Definition B: the wanted handles in, one fact for each out.

    A lookup that fails is a fact too (``LOOKUP_FAILED``, no push time, commit
    not found), so the decision weighs it as unresolved instead of asking again.
    """

    def __init__(
        self,
        github: ProtocolReconcileGitHub | None = None,
        git: ProtocolReconcileGit | None = None,
        host: ProtocolReconcileHost | None = None,
    ) -> None:
        self._github: ProtocolReconcileGitHub = (
            github if github is not None else GhPullRequests()
        )
        self._git: ProtocolReconcileGit = git if git is not None else GitCommits()
        self._host: ProtocolReconcileHost = (
            host if host is not None else LocalReconcileHost()
        )

    async def handle(
        self, request: ModelVerifyEvidenceRequest
    ) -> ModelVerifyEvidenceResult:
        try:
            root = (
                Path(request.registry_root)
                if request.registry_root
                else self._host.registry_root()
            )
        except (ReconcilePortError, KeyError) as exc:
            return ModelVerifyEvidenceResult(
                correlation_id=request.correlation_id,
                error=f"registry root unreadable: {exc}",
            )
        org = request.github_org
        wanted = request.wanted
        gate = asyncio.Semaphore(MAX_CONCURRENT_LOOKUPS)

        async def look[T](call: Callable[[], T]) -> T:
            async with gate:
                return await asyncio.to_thread(call)

        prs, shas, pushes = await asyncio.gather(
            asyncio.gather(
                *(
                    look(partial(self._github.pr, org, r.repo, r.number))
                    for r in wanted.prs
                )
            ),
            asyncio.gather(
                *(
                    look(partial(self._git.probe, root, request.registry_name, r))
                    for r in wanted.shas
                )
            ),
            asyncio.gather(
                *(
                    look(partial(self._github.pushed_at, org, r.repo, r.number))
                    for r in wanted.pushes
                )
            ),
        )
        return ModelVerifyEvidenceResult(
            correlation_id=request.correlation_id,
            facts=ModelReconcileFacts(
                prs=tuple(prs), shas=tuple(shas), pushes=tuple(pushes)
            ),
        )
