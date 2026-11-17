# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answer one of five typed questions over the work ledger graph of a window.

Reads the window's rows from work_ledger_rows, derives the graph with the shared
pure derivation (the same one node_work_ledger_graph_compute runs), and walks it.
A read failure, an empty window or a graph that does not hold together is an
error answer, never an empty one.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.handlers.work_ledger_graph_derive import derive_graph, graph_digest
from omnimarket.models.work_ledger_graph import (
    ModelWorkLedgerGraph,
    ModelWorkLedgerGraphQueryRequest,
    ModelWorkLedgerGraphQueryResult,
)
from omnimarket.nodes.node_work_ledger_graph_query_effect.handlers.work_ledger_graph_queries import (
    WorkLedgerGraphQueryError,
    answer,
)
from omnimarket.nodes.node_work_ledger_graph_query_effect.handlers.work_ledger_graph_reader import (
    PostgresWorkLedgerGraphReader,
    ProtocolWorkLedgerGraphReader,
    load_graph_source,
)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


class HandlerWorkLedgerGraphQuery:
    def __init__(
        self,
        *,
        contract_path: Path | None = None,
        reader: ProtocolWorkLedgerGraphReader | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        source = load_graph_source(contract_path or _CONTRACT_PATH)
        self._window = timedelta(days=source.default_window_days)
        self._reader = (
            reader if reader is not None else PostgresWorkLedgerGraphReader(source)
        )
        self._now = now if now is not None else (lambda: datetime.now(UTC))

    def handle(
        self, request: ModelWorkLedgerGraphQueryRequest
    ) -> ModelWorkLedgerGraphQueryResult:
        if not isinstance(request, ModelWorkLedgerGraphQueryRequest):
            raise TypeError(
                "work ledger graph query handler requires its contract payload"
            )
        started = time.monotonic()
        until = request.until or self._now()
        since = request.since or (until - self._window)
        base = {
            "correlation_id": request.correlation_id,
            "query": request.query,
            "window_since": since,
            "window_until": until,
        }
        try:
            rows = self._reader.read_rows(since, until)
        except Exception as exc:
            return ModelWorkLedgerGraphQueryResult(
                **base,
                status="error",
                error=f"graph unreadable: work_ledger_rows read failed: {type(exc).__name__}: {exc}",
                elapsed_seconds=time.monotonic() - started,
            )
        return self.answer_graph(request, derive_graph(rows), base, started)

    def answer_graph(
        self,
        request: ModelWorkLedgerGraphQueryRequest,
        graph: ModelWorkLedgerGraph,
        base: dict[str, object] | None = None,
        started: float | None = None,
    ) -> ModelWorkLedgerGraphQueryResult:
        started = time.monotonic() if started is None else started
        fields: dict[str, object] = base or {
            "correlation_id": request.correlation_id,
            "query": request.query,
            "window_since": request.since,
            "window_until": request.until,
        }
        try:
            if graph_digest(graph.nodes, graph.edges, graph.row_count) != graph.digest:
                raise WorkLedgerGraphQueryError(
                    "graph unreadable: digest does not match its content"
                )
            items, rule = answer(graph, request)
        except WorkLedgerGraphQueryError as exc:
            return ModelWorkLedgerGraphQueryResult(
                **fields,
                status="error",
                error=str(exc),
                rows_read=graph.row_count,
                elapsed_seconds=time.monotonic() - started,
            )
        return ModelWorkLedgerGraphQueryResult(
            **fields,
            status="ok",
            rows_read=graph.row_count,
            graph_digest=graph.digest,
            open_rule=rule,
            matched=len(items),
            items=tuple(items[: request.limit]),
            elapsed_seconds=time.monotonic() - started,
        )


__all__ = ["HandlerWorkLedgerGraphQuery"]
