# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The production compaction binding wires the projection read adapters (OMN-21099)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_context_compaction_orchestrator.handlers import (
    compaction_runtime_binding as binding_module,
)

from .test_session_barrier_wait import _request

pytestmark = pytest.mark.unit


def test_default_binding_reads_projected_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = binding_module.load_compaction_runtime_binding()
    request = _request()
    assert request.barrier_id is not None
    emitted_at = datetime(2026, 10, 23, 13, 37, 41, tzinfo=UTC)
    rows: list[tuple[object, ...]] = [
        (
            stream,
            sequence,
            None,
            None,
            None,
            None,
            "bound-lane" if sequence == 3 else None,
        )
        for stream in ("lineage", "content")
        for sequence in (1, 2, 3)
    ]
    rows.extend(
        (stream, None, request.barrier_id, 3, None, emitted_at, None)
        for stream in ("lineage", "content")
    )
    executions: list[tuple[object, tuple[object, ...]]] = []
    connections: list[_Connection] = []
    connect_calls: list[tuple[str, dict[str, object]]] = []

    class _Cursor:
        def execute(self, query: object, params: tuple[object, ...]) -> None:
            executions.append((query, params))

        def fetchall(self) -> list[tuple[object, ...]]:
            return rows

    class _Connection:
        def __init__(self) -> None:
            self.closed = False

        @contextmanager
        def cursor(self) -> Iterator[_Cursor]:
            yield _Cursor()

        def close(self) -> None:
            self.closed = True

    def connect(dsn: str, **kwargs: object) -> _Connection:
        connect_calls.append((dsn, kwargs))
        conn = _Connection()
        connections.append(conn)
        return conn

    dsn = "fixture-barrier-binding-dsn"
    monkeypatch.setenv(binding.barrier_probe.dsn_env, dsn)
    # Allow the regression to reach the unwired read before this import exists.
    monkeypatch.setattr(binding_module, "connect_read_only", connect, raising=False)
    wait, source = binding_module.bind_compaction_ports(binding)

    assert wait.wait(request).outcome == "met"
    probe = source.read(request.session_id, request.barrier_id)
    assert probe.source_revision == emitted_at.isoformat().replace("+00:00", "Z")
    assert probe.lane == "bound-lane"
    assert connect_calls == [(dsn, {"connect_timeout": 3})] * 2
    assert len(connections) == 2
    assert all(conn.closed for conn in connections)
    assert [params for _, params in executions] == [
        (str(binding.barrier_probe.statement_timeout_ms),),
        ("session-1", "session-1", request.barrier_id),
    ] * 2
