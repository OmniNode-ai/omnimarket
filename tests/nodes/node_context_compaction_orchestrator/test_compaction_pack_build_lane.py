# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The compaction pack command names the session's lane (OMN-21334, TA.11b).

The work-state tiers (open_work, orchestration_state) match ledger rows by the
caller scope's lane. A compaction command carries only a session id, so the
orchestrator takes the lane the session's own lineage rows record from the
barrier probe and puts it in the pack command's caller scope.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from omnibase_core.models.context.model_context_access import ModelContextAccess

from omnimarket.models.context_pack_orchestrator import ModelContextPackCommand
from omnimarket.models.model_session_stream_barrier import (
    ModelSessionStreamBarrierProbe,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.handler_compaction_pack_build import (
    build_pack_command,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.session_barrier_probe import (
    ModelSessionBarrierReadBinding,
    PostgresSessionBarrierProbeSource,
)

from .bus_fixture import MetSource
from .test_compaction_pack_build import _pack_orchestrator, _request, _sources

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("promoted_context_profile_routing"),
]

_REVISION = "2026-10-23T12:00:00Z"
_STAMP = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


class _LaneSource(MetSource):
    def read(self, session_id: str, barrier_id: str) -> ModelSessionStreamBarrierProbe:
        return (
            super()
            .read(session_id, barrier_id)
            .model_copy(update={"lane": "cmod-lane-1", "source_revision": _REVISION})
        )


def test_pack_command_caller_scope_carries_the_lane() -> None:
    access = ModelContextAccess(tenant_id="lab")
    with_lane = build_pack_command(_request(), access, _REVISION, lane="cmod-lane-1")
    without = build_pack_command(_request(), access, _REVISION)
    assert with_lane.request.caller_scope.lane == "cmod-lane-1"
    assert without.request.caller_scope.lane is None


@pytest.mark.asyncio
async def test_orchestrator_puts_the_probed_lane_in_the_published_command(
    compaction_bus,
) -> None:
    bus = compaction_bus(_pack_orchestrator(_sources()))
    output = await bus.handler(barrier_probe_source=_LaneSource()).handle_async(
        _request()
    )
    assert output.events[-1].status == "built"
    (envelope,) = bus.commands
    command = ModelContextPackCommand.model_validate(envelope["payload"])
    assert command.request.caller_scope.lane == "cmod-lane-1"


class _Cursor:
    def __init__(self, rows: list[tuple[Any, ...]], statements: list[str]) -> None:
        self._rows = rows
        self._statements = statements

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, statement: object, params: object = None) -> None:
        self._statements.append(str(statement))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Connection:
    def __init__(self, rows: list[tuple[Any, ...]], statements: list[str]) -> None:
        self._rows = rows
        self._statements = statements

    def cursor(self) -> _Cursor:
        return _Cursor(self._rows, self._statements)

    def close(self) -> None:
        return None


class _Composed:
    def __init__(self, text: str) -> None:
        self._text = text

    def format(self, **parts: object) -> str:
        return self._text.format(**parts)


def _identifier(*parts: str) -> str:
    return ".".join(parts)


#: The two composer calls the probe makes, rendered as plain text.
_SQL = SimpleNamespace(Identifier=_identifier, SQL=_Composed)


def _connect(
    rows: list[tuple[Any, ...]], statements: list[str]
) -> Callable[..., _Connection]:
    def connect(*_args: object, **_kwargs: object) -> _Connection:
        return _Connection(rows, statements)

    return connect


def _read(
    rows: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> tuple[ModelSessionStreamBarrierProbe, list[str]]:
    monkeypatch.setenv("LANE_TEST_DSN", "dsn")
    statements: list[str] = []
    source = PostgresSessionBarrierProbeSource(
        ModelSessionBarrierReadBinding(
            dsn_env="LANE_TEST_DSN",
            lineage_relation="s.lineage",
            content_relation="s.content",
        ),
        connect=_connect(rows, statements),
        sql_composer=_SQL,
    )
    return source.read("session-1", "barrier-1"), statements


def _rows(*lineage: tuple[int, str | None]) -> list[tuple[Any, ...]]:
    return [
        *(("lineage", seq, None, None, None, None, lane) for seq, lane in lineage),
        ("lineage", None, "barrier-1", len(lineage), "enabled", _STAMP, None),
        ("content", 1, None, None, None, None, None),
        ("content", None, "barrier-1", 1, None, _STAMP, None),
    ]


def test_probe_reports_the_newest_lane_the_lineage_records(monkeypatch) -> None:
    probe, statements = _read(
        _rows((1, "old-lane"), (3, "new-lane"), (2, None)), monkeypatch
    )
    assert probe.lane == "new-lane"
    assert "lane" in statements[-1]


def test_probe_lane_is_none_when_no_lineage_row_names_one(monkeypatch) -> None:
    probe, _ = _read(_rows((1, None), (2, None)), monkeypatch)
    assert probe.lane is None
