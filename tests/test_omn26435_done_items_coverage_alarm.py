# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for OMN-26435 done-items coverage alarm."""

from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr

from omnimarket.nodes.node_projection_freshness_alert_effect.handlers.classify_done_items_coverage import (
    classify_done_items_coverage,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.handlers.freshness_reader import (
    PostgresFreshnessReader,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.handlers.handler_projection_freshness_alert import (
    HandlerProjectionFreshnessAlert,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.models import (
    EnumFreshnessState,
    ModelCoverageCheck,
    ModelCoverageObservation,
    ModelFreshnessCheck,
    ModelFreshnessObservation,
    ModelFreshnessTrigger,
)
from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (
    ModelSlackPublish,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 22, 9, tzinfo=UTC)
CORRELATION = uuid.UUID("12345678-1234-1234-1234-123456789abc")

HANDLER_MODULE = importlib.import_module(HandlerProjectionFreshnessAlert.__module__)


class _Clock:
    def __call__(self) -> float:
        return NOW.timestamp()


class _Publisher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []

    def __call__(self, topic: str, payload: bytes) -> None:
        self.calls.append((topic, payload))


class _Reader:
    def __init__(
        self,
        coverage: ModelCoverageObservation,
        error: Exception | None = None,
    ) -> None:
        self._coverage = coverage
        self._error = error
        self.coverage_checks: list[ModelCoverageCheck] = []

    def read(self, check: ModelFreshnessCheck) -> ModelFreshnessObservation:
        return ModelFreshnessObservation(supply_newest_at=NOW, demand_distinct_active=0)

    def read_coverage(self, check: ModelCoverageCheck) -> ModelCoverageObservation:
        self.coverage_checks.append(check)
        if self._error is not None:
            raise self._error
        return self._coverage


class _FakeCursor:
    def __init__(self) -> None:
        self._fetchall_results: list[list[tuple]] = [
            [
                (
                    f"{(NOW - timedelta(seconds=3600)).strftime('%Y-%m-%dT%H:%M:%SZ')} | TERMINAL | lane=alpha | ticket=OMN-1 | outcome=done | finished",
                )
            ],
            [],
        ]
        self._fetchone_results: list[tuple] = [(NOW,), (0,)]
        self._fetchall_index = 0
        self._fetchone_index = 0

    def execute(self, _sql: str, *_params: object) -> None:
        pass

    def fetchall(self) -> list[tuple]:
        if self._fetchall_index < len(self._fetchall_results):
            result = self._fetchall_results[self._fetchall_index]
            self._fetchall_index += 1
            return result
        return []

    def fetchone(self) -> tuple | None:
        if self._fetchone_index < len(self._fetchone_results):
            result = self._fetchone_results[self._fetchone_index]
            self._fetchone_index += 1
            return result
        return None

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        pass


class _FakeConnection:
    def __init__(self) -> None:
        self.closed = 0
        self._cursor = _FakeCursor()

    def cursor(self) -> _FakeCursor:
        return self._cursor


@pytest.fixture(autouse=True)
def _patch_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        HANDLER_MODULE,
        "resolve_api_key_loop_safe",
        lambda _ref, **_kw: SecretStr("C0TESTCHANNEL"),
    )


def test_done_items_alarm_fires_on_unprojected_done_event() -> None:
    coverage = ModelCoverageObservation(demand_done_tickets=1, unprojected_tickets=1)
    reader = _Reader(coverage)
    publisher = _Publisher()
    handler = HandlerProjectionFreshnessAlert(
        reader=reader,
        event_publisher=publisher,
        clock=_Clock(),
    )
    trigger = ModelFreshnessTrigger(correlation_id=CORRELATION)
    evaluation = handler.handle(trigger)

    verdict = next(
        v for v in evaluation.coverage_verdicts if v.check_id == "done_items_coverage"
    )
    assert verdict.state == EnumFreshnessState.STALE
    assert all(v.check_id != "done_items_coverage" for v in evaluation.verdicts)

    matching = [
        (topic, payload)
        for topic, payload in publisher.calls
        if b"done_items_coverage" in payload
    ]
    assert len(matching) == 1
    _topic, payload = matching[0]
    model = ModelSlackPublish.model_validate_json(payload)
    assert model.idempotency_key.startswith(
        "projection-freshness|done_items_coverage|STALE|"
    )
    assert model.channel == "C0TESTCHANNEL"


def test_done_items_alarm_quiet_without_done_events() -> None:
    coverage = ModelCoverageObservation(demand_done_tickets=0, unprojected_tickets=0)
    reader = _Reader(coverage)
    publisher = _Publisher()
    handler = HandlerProjectionFreshnessAlert(
        reader=reader,
        event_publisher=publisher,
        clock=_Clock(),
    )
    trigger = ModelFreshnessTrigger(correlation_id=CORRELATION)
    evaluation = handler.handle(trigger)

    verdict = next(
        v for v in evaluation.coverage_verdicts if v.check_id == "done_items_coverage"
    )
    assert verdict.state == EnumFreshnessState.IDLE
    assert all(v.check_id != "done_items_coverage" for v in evaluation.verdicts)

    matching = [
        (topic, payload)
        for topic, payload in publisher.calls
        if b"done_items_coverage" in payload
    ]
    assert matching == []


def test_done_items_alarm_unknown_when_source_unreadable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    coverage = ModelCoverageObservation(demand_done_tickets=1, unprojected_tickets=1)
    reader = _Reader(coverage, error=OSError("dark source"))
    publisher = _Publisher()
    handler = HandlerProjectionFreshnessAlert(
        reader=reader,
        event_publisher=publisher,
        clock=_Clock(),
    )
    trigger = ModelFreshnessTrigger(correlation_id=CORRELATION)
    evaluation = handler.handle(trigger)

    verdict = next(
        v for v in evaluation.coverage_verdicts if v.check_id == "done_items_coverage"
    )
    assert verdict.state == EnumFreshnessState.UNKNOWN
    assert "OSError" in (verdict.error or "")

    matching = [
        (topic, payload)
        for topic, payload in publisher.calls
        if b"done_items_coverage" in payload
    ]
    assert matching == []

    error_records = [
        r
        for r in caplog.records
        if r.levelname == "ERROR" and "Done-items coverage UNKNOWN" in r.getMessage()
    ]
    assert error_records, (
        "expected an ERROR record mentioning 'Done-items coverage UNKNOWN'"
    )


def test_done_items_alarm_unknown_when_ledger_rows_refused() -> None:
    coverage = ModelCoverageObservation(
        demand_done_tickets=0,
        unprojected_tickets=0,
        refused_rows=3,
    )
    reader = _Reader(coverage)
    publisher = _Publisher()
    handler = HandlerProjectionFreshnessAlert(
        reader=reader,
        event_publisher=publisher,
        clock=_Clock(),
    )
    trigger = ModelFreshnessTrigger(correlation_id=CORRELATION)
    evaluation = handler.handle(trigger)

    verdict = next(
        v for v in evaluation.coverage_verdicts if v.check_id == "done_items_coverage"
    )
    assert verdict.state == EnumFreshnessState.UNKNOWN

    matching = [
        (topic, payload)
        for topic, payload in publisher.calls
        if b"done_items_coverage" in payload
    ]
    assert matching == []


def test_done_items_coverage_thresholds_come_from_the_contract(tmp_path: Path) -> None:
    contract_path = Path(HANDLER_MODULE.__file__).parent.parent / "contract.yaml"
    contract = yaml.safe_load(contract_path.read_text())
    contract["coverage_checks"][0]["max_age_seconds"] = 7200
    contract["coverage_checks"][0]["lookback_seconds"] = 172800
    written_path = tmp_path / "contract.yaml"
    written_path.write_text(yaml.safe_dump(contract))

    coverage = ModelCoverageObservation(demand_done_tickets=0, unprojected_tickets=0)
    reader = _Reader(coverage)
    publisher = _Publisher()
    handler = HandlerProjectionFreshnessAlert(
        reader=reader,
        event_publisher=publisher,
        clock=_Clock(),
        contract_path=written_path,
    )
    trigger = ModelFreshnessTrigger(correlation_id=CORRELATION)
    handler.handle(trigger)

    assert len(reader.coverage_checks) == 1
    check = reader.coverage_checks[0]
    assert check.max_age_seconds == 7200
    assert check.lookback_seconds == 172800


def test_done_items_alarm_end_to_end_from_a_ledger_row() -> None:
    fake_conn = _FakeConnection()
    reader = PostgresFreshnessReader(dsn="postgresql://unused", now=lambda: NOW)
    reader._conn = fake_conn
    publisher = _Publisher()
    handler = HandlerProjectionFreshnessAlert(
        reader=reader,
        event_publisher=publisher,
        clock=_Clock(),
    )
    trigger = ModelFreshnessTrigger(correlation_id=CORRELATION)
    evaluation = handler.handle(trigger)

    verdict = next(
        v for v in evaluation.coverage_verdicts if v.check_id == "done_items_coverage"
    )
    assert verdict.state == EnumFreshnessState.STALE
    assert verdict.demand_done_tickets == 1
    assert verdict.unprojected_tickets == 1

    matching = [
        (topic, payload)
        for topic, payload in publisher.calls
        if b"done_items_coverage" in payload
    ]
    assert len(matching) == 1


@pytest.mark.parametrize(
    ("demand", "unprojected", "refused_rows", "error", "expected"),
    [
        (1, 1, 0, None, EnumFreshnessState.STALE),
        (3, 0, 0, None, EnumFreshnessState.FRESH),
        (0, 0, 0, None, EnumFreshnessState.IDLE),
        (None, 0, 0, None, EnumFreshnessState.UNKNOWN),
        (2, None, 0, None, EnumFreshnessState.UNKNOWN),
        (1, 1, 0, "dark source", EnumFreshnessState.UNKNOWN),
        (1, 0, 2, None, EnumFreshnessState.UNKNOWN),
        (0, 0, 1, None, EnumFreshnessState.UNKNOWN),
        (2, 2, 1, None, EnumFreshnessState.STALE),
        (3, 0, 1, None, EnumFreshnessState.UNKNOWN),
    ],
)
def test_done_items_classifier_states(
    demand: int | None,
    unprojected: int | None,
    refused_rows: int,
    error: str | None,
    expected: EnumFreshnessState,
) -> None:
    verdict = classify_done_items_coverage(
        check_id="done_items_coverage",
        demand_done_tickets=demand,
        unprojected_tickets=unprojected,
        refused_rows=refused_rows,
        error=error,
    )
    assert verdict.check_id == "done_items_coverage"
    assert verdict.state == expected
