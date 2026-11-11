# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-state projection reads its ledger source through the bus read command the overlay names (OMN-23691).

Slice one of the work-state sources the runtime could not read: the ledger. ``run`` takes a ledger source the caller built from
the overlay's route, reads only through it (the file route is not a fallback), and names the source unread with the cause when
the command refuses, answers truncated or is not configured. The failing control is the old route: no ledger source and no
ledger file reads the source unread, so the fresh reading comes from the route and nothing else.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.work_state_projection import (
    EnumSourceReading,
    EnumWorkStateSource,
    ModelWorkStateResult,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    build_row_source,
)
from omnimarket.nodes.node_work_state_projection_effect.runtime_work_state import run
from omnimarket.nodes.node_work_state_projection_scheduled_effect.protocols.adapter_file_sources_projector import (
    FileSourcesWorkStateProjector,
)
from tests.nodes.node_work_state_projection_scheduled_effect.ledger_route_support import (
    COMMAND_ENV,
    FILE_ROUTE_ENV,
    ROUTE,
    FakeBusReadCommand,
    environment,
    install_fake,
    ledger_file,
    ledger_line,
    snapshots_text,
    unread_for,
)
from tests.nodes.node_work_state_projection_scheduled_effect.support import T0

pytestmark = pytest.mark.unit


def ledger_status(result: ModelWorkStateResult) -> tuple[EnumSourceReading, str]:
    status = next(
        s for s in result.source_status if s.source is EnumWorkStateSource.LEDGER
    )
    return status.reading, status.cause


def test_golden_ledger_rows_come_through_the_bus_read_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_fake(
        monkeypatch,
        FakeBusReadCommand([ledger_line("CLAIM", "lane-a", "ZEP-1")]),
    )
    env = environment(tmp_path)
    result = run(env, T0, ledger_source=build_row_source(ROUTE, env))
    assert ledger_status(result)[0] is EnumSourceReading.FRESH
    assert unread_for(result.unread, "ledger") == []
    assert {k for request in fake.requests for k in request["kinds"]} >= {"CLAIM"}
    assert "lane-a" in snapshots_text(tmp_path)


def test_golden_the_ledger_file_is_never_read_when_a_route_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_fake(
        monkeypatch,
        FakeBusReadCommand([ledger_line("CLAIM", "lane-a", "ZEP-1")]),
    )
    env = environment(
        tmp_path,
        ledger_file=ledger_file(tmp_path, ledger_line("CLAIM", "file-lane", "ZEP-9")),
    )
    result = run(env, T0, ledger_source=build_row_source(ROUTE, env))
    assert fake.requests
    assert "file-lane" not in snapshots_text(tmp_path)
    assert "file-lane" not in result.model_dump_json()


def test_error_unread_a_refusing_command_names_the_ledger_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake(monkeypatch, FakeBusReadCommand([], exit_code=1))
    env = environment(
        tmp_path,
        ledger_file=ledger_file(tmp_path, ledger_line("CLAIM", "file-lane", "ZEP-9")),
    )
    result = run(env, T0, ledger_source=build_row_source(ROUTE, env))
    reading, cause = ledger_status(result)
    assert reading is EnumSourceReading.UNREAD
    assert "CLAIM read-command-exit-1:WorkLedgerQueryError" in cause
    assert len(unread_for(result.unread, "ledger")) == 1
    assert "file-lane" not in result.model_dump_json()


def test_error_unread_a_truncated_answer_is_unread_never_a_short_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake(
        monkeypatch,
        FakeBusReadCommand([ledger_line("CLAIM", "lane-a", "ZEP-1")], matched=5),
    )
    env = environment(tmp_path)
    result = run(env, T0, ledger_source=build_row_source(ROUTE, env))
    reading, cause = ledger_status(result)
    assert reading is EnumSourceReading.UNREAD
    assert "read-answer-truncated" in cause


def test_error_unread_a_command_that_is_not_configured_is_named_and_raises_nothing(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path, with_command=False)
    result = FileSourcesWorkStateProjector(env, str(tmp_path), ROUTE).project(T0)
    reading, cause = ledger_status(result)
    assert reading is EnumSourceReading.UNREAD
    assert "ledger-route-unconfigured" in cause
    assert COMMAND_ENV in cause


def test_failing_control_without_a_route_the_ledger_reads_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_fake(
        monkeypatch,
        FakeBusReadCommand([ledger_line("CLAIM", "lane-a", "ZEP-1")]),
    )
    result = run(environment(tmp_path), T0)
    reading, cause = ledger_status(result)
    assert reading is EnumSourceReading.UNREAD
    assert f"{FILE_ROUTE_ENV} unset" in cause
    assert fake.requests == []
