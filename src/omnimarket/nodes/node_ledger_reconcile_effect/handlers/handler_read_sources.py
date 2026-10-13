# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the ledger host: ledger, archives, clone registry, roster, overlay and the clock (OMN-20677)."""

from __future__ import annotations

from pathlib import Path

from omnimarket.models.ledger_reconcile import ModelReconcileSources
from omnimarket.models.ledger_reconcile.model_ledger_reconcile_ops import (
    ModelReadSourcesRequest,
    ModelReadSourcesResult,
)

from ..protocols import (
    ProtocolReconcileClock,
    ProtocolReconcileHost,
    ReconcilePortError,
)
from ..protocols.local_ledger_reconcile_adapters import LocalReconcileHost, SystemClock


class HandlerReadSources:
    """Definition B: one request, one result that either holds the sources or says why not."""

    def __init__(
        self,
        host: ProtocolReconcileHost | None = None,
        clock: ProtocolReconcileClock | None = None,
    ) -> None:
        self._host: ProtocolReconcileHost = (
            host if host is not None else LocalReconcileHost()
        )
        self._clock: ProtocolReconcileClock = (
            clock if clock is not None else SystemClock()
        )

    async def handle(self, request: ModelReadSourcesRequest) -> ModelReadSourcesResult:
        blocked = self._host.prerequisites()
        if blocked:
            return ModelReadSourcesResult(
                correlation_id=request.correlation_id, error=blocked
            )
        try:
            ledger = (
                Path(request.ledger_path)
                if request.ledger_path
                else self._host.ledger_path()
            )
            root = (
                Path(request.registry_root)
                if request.registry_root
                else self._host.registry_root()
            )
            roster = set(request.live_lanes)
            if request.live_lanes_file is not None:
                roster |= self._host.read_roster(Path(request.live_lanes_file))
            live, archives = self._host.read_ledger(ledger)
            sources = ModelReconcileSources(
                live=live,
                archives=archives,
                clones=self._host.clone_names(root),
                registry_name=root.name,
                live_lanes=tuple(sorted(roster)),
                overlay=self._host.overlay(),
                read_at=self._clock.now(),
            )
        except (ReconcilePortError, OSError) as exc:
            return ModelReadSourcesResult(
                correlation_id=request.correlation_id, error=str(exc)
            )
        return ModelReadSourcesResult(
            correlation_id=request.correlation_id, sources=sources
        )
