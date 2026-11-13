# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 and AC4: an unreadable source judges nothing; a missing overlay refuses the evaluation."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_work_progress_alert_effect import NodeWorkProgressAlertEffect
from omnimarket.nodes.node_work_progress_alert_effect.handlers.ledger_row_reader import (
    PostgresLedgerRowReader,
    resolve_dsn,
)
from omnimarket.nodes.node_work_progress_alert_effect.models import (
    EnumWorkProgressOutcome,
    ModelWorkProgressAlertTrigger,
)
from tests.node_overlay_support import install_node_overlay
from tests.nodes.node_work_progress_alert_effect.support import (
    BUDGETS,
    COMPUTE,
    EFFECT,
    SETTINGS,
    Clock,
    Publisher,
    Reader,
    install_overlays,
    resolve_channel,
    stalled_lane,
)


def test_failing_reader_emits_nothing_and_the_terminal_event_names_the_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    install_overlays(monkeypatch, tmp_path)
    resolve_channel(monkeypatch)
    publisher = Publisher()
    node = NodeWorkProgressAlertEffect(
        reader=Reader(stalled_lane(), error="connection refused"),
        event_publisher=publisher,
        clock=Clock(),
    )
    with caplog.at_level(logging.ERROR):
        result = node.handle(ModelWorkProgressAlertTrigger())
    assert result.outcome is EnumWorkProgressOutcome.UNREADABLE
    assert result.reason == "RuntimeError: connection refused"
    assert (result.subjects, result.findings, result.commands) == (0, 0, 0)
    assert publisher.calls == []
    assert "connection refused" in caplog.text


def test_unset_dsn_is_an_unreadable_source_not_a_wiring_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_overlays(monkeypatch, tmp_path)
    monkeypatch.delenv("OMNINODE_INTERNAL_DB_URL", raising=False)
    node = NodeWorkProgressAlertEffect(event_publisher=Publisher(), clock=Clock())
    result = node.handle(ModelWorkProgressAlertTrigger())
    assert result.outcome is EnumWorkProgressOutcome.UNREADABLE
    assert "OMNINODE_INTERNAL_DB_URL" in (result.reason or "")


def test_a_recovered_source_is_read_in_the_next_interval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_overlays(monkeypatch, tmp_path)
    resolve_channel(monkeypatch)
    clock, publisher = Clock(), Publisher()
    reader = Reader(stalled_lane(), error="down")
    node = NodeWorkProgressAlertEffect(
        reader=reader, event_publisher=publisher, clock=clock
    )
    assert node.handle(ModelWorkProgressAlertTrigger()).outcome is (
        EnumWorkProgressOutcome.UNREADABLE
    )
    reader.error = None
    clock.advance(301)
    assert node.handle(ModelWorkProgressAlertTrigger()).findings == 1


def test_no_overlay_constructs_then_refuses_the_evaluation_naming_the_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    publisher = Publisher()
    node = NodeWorkProgressAlertEffect(
        reader=Reader(stalled_lane()), event_publisher=publisher, clock=Clock()
    )
    result = node.handle(ModelWorkProgressAlertTrigger())
    assert result.outcome is EnumWorkProgressOutcome.REFUSED
    assert f"{EFFECT}/overlay.yaml" in (result.reason or "")
    assert publisher.calls == []


def test_overlay_is_read_at_evaluation_time_not_at_wiring(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    resolve_channel(monkeypatch)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    publisher = Publisher()
    node = NodeWorkProgressAlertEffect(
        reader=Reader(stalled_lane()), event_publisher=publisher, clock=Clock()
    )
    # The overlay appears only after the node was constructed.
    install_overlays(monkeypatch, tmp_path)
    assert node.handle(ModelWorkProgressAlertTrigger()).findings == 1


def test_missing_budget_overlay_refuses_and_names_the_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(monkeypatch, tmp_path, EFFECT, SETTINGS)
    publisher = Publisher()
    node = NodeWorkProgressAlertEffect(
        reader=Reader(stalled_lane()), event_publisher=publisher, clock=Clock()
    )
    result = node.handle(ModelWorkProgressAlertTrigger())
    assert result.outcome is EnumWorkProgressOutcome.REFUSED
    assert f"{COMPUTE}/overlay.yaml" in (result.reason or "")
    assert result.check_interval_seconds == 300
    assert publisher.calls == []


def test_a_budget_missing_a_phase_refuses_naming_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_overlays(
        monkeypatch,
        tmp_path,
        budgets={"budget_seconds": {"placed": 900, "claimed": 60}},
    )
    result = NodeWorkProgressAlertEffect(
        reader=Reader(stalled_lane()), event_publisher=Publisher(), clock=Clock()
    ).handle(ModelWorkProgressAlertTrigger())
    assert result.outcome is EnumWorkProgressOutcome.REFUSED
    assert "budget_seconds.pr_open" in (result.reason or "")


@pytest.mark.parametrize(
    "bad",
    [
        {**SETTINGS, "lookback_seconds": 0},
        {**SETTINGS, "check_interval_seconds": "300"},
        {k: v for k, v in SETTINGS.items() if k != "ignore"},
        {**SETTINGS, "surprise": 1},
    ],
)
def test_malformed_settings_overlay_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad: dict[str, object]
) -> None:
    install_overlays(monkeypatch, tmp_path, settings=bad)
    result = NodeWorkProgressAlertEffect(
        reader=Reader(stalled_lane()), event_publisher=Publisher(), clock=Clock()
    ).handle(ModelWorkProgressAlertTrigger())
    assert result.outcome is EnumWorkProgressOutcome.REFUSED
    assert EFFECT in (result.reason or "")


def test_no_setting_value_is_packaged() -> None:
    from importlib.resources import files

    package = files(f"omnimarket.nodes.{EFFECT}")
    contract = yaml.safe_load(package.joinpath("contract.yaml").read_text("utf-8"))
    for name in ("check_interval_seconds", "lookback_seconds", "ignore"):
        assert name not in contract
    assert yaml.safe_load(package.joinpath("overlay.example.yaml").read_text("utf-8"))
    assert BUDGETS  # the budgets belong to the compute node's overlay


class _Cursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.executed.append((sql, params))

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows


class _Connection:
    closed = 0

    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.close_calls = 0

    def cursor(self) -> _Cursor:
        return self._cursor

    def close(self) -> None:
        self.close_calls += 1


def test_postgres_reader_runs_one_parameterised_query() -> None:
    from tests.nodes.node_work_progress_alert_effect.support import NOW

    cursor = _Cursor([("rid", NOW, "CLAIM", "lane-a", "raw")])
    reader = PostgresLedgerRowReader(dsn="postgresql://192.0.2.1/x")
    reader._conn = _Connection(cursor)
    rows = reader.read(NOW)
    assert [r.row_id for r in rows] == ["rid"]
    [(sql, params)] = cursor.executed
    assert params == (NOW,)
    assert "omninode_internal.work_ledger_rows" in sql
    assert "'CLAIM', 'STATUS', 'TERMINAL'" in sql
    assert "%s" in sql
    assert str(NOW.year) not in sql


def test_postgres_reader_drops_its_connection_after_an_error() -> None:
    from tests.nodes.node_work_progress_alert_effect.support import NOW

    class Boom(_Cursor):
        def execute(self, sql: str, params: tuple[object, ...]) -> None:
            raise RuntimeError("boom")

    connection = _Connection(Boom([]))
    reader = PostgresLedgerRowReader(dsn="postgresql://192.0.2.1/x")
    reader._conn = connection
    with pytest.raises(RuntimeError, match="boom"):
        reader.read(NOW)
    assert connection.close_calls == 1
    assert reader._conn is None


def test_resolve_dsn_requires_the_contract_named_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNINODE_INTERNAL_DB_URL", " ")
    with pytest.raises(RuntimeError, match="OMNINODE_INTERNAL_DB_URL"):
        resolve_dsn("OMNINODE_INTERNAL_DB_URL")
    monkeypatch.setenv("OMNINODE_INTERNAL_DB_URL", "postgresql://192.0.2.1/x")
    assert resolve_dsn("OMNINODE_INTERNAL_DB_URL").startswith("postgresql://")
