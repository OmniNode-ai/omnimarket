# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Derive the work ledger graph from supplied rows. Pure: no I/O, no clock, no model."""

from __future__ import annotations

from omnimarket.handlers.work_ledger_graph_derive import derive_graph
from omnimarket.models.work_ledger_graph import (
    ModelWorkLedgerGraph,
    ModelWorkLedgerGraphRequest,
)


class HandlerWorkLedgerGraph:
    def handle(self, request: ModelWorkLedgerGraphRequest) -> ModelWorkLedgerGraph:
        if not isinstance(request, ModelWorkLedgerGraphRequest):
            raise TypeError("work ledger graph handler requires its contract payload")
        return derive_graph(request.rows)


__all__ = ["HandlerWorkLedgerGraph"]
