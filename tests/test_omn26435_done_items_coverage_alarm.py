# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for OMN-26435 done-items coverage alarm."""

from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from omnimarket.nodes.node_projection_freshness_alert_effect.handlers.classify_done_items_coverage import (
    classify_done_items_coverage,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.handlers.handler_projection_freshness_alert import (
    HandlerProjectionFreshnessAlert,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.models import (
    EnumFreshnessState,
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
    def __init__(self, coverage: ModelCoverageObservation) -> None:
        self._coverage = coverage

    def read(self, check: ModelFreshnessCheck) -> ModelFreshnessObservation:
        return ModelFreshnessObservation(supply_newest_at=NOW, demand_distinct_active=0)

    def read_coverage(self, check: ModelFreshnessCheck) -> ModelCoverageObservation:
        return self._coverage


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


@pytest.mark.parametrize(
    ("demand", "unprojected", "error", "expected"),
    [
        (1, 1, None, EnumFreshnessState.STALE),
        (3, 0, None, EnumFreshnessState.FRESH),
        (0, 0, None, EnumFreshnessState.IDLE),
        (None, 0, None, EnumFreshnessState.UNKNOWN),
        (2, None, None, EnumFreshnessState.UNKNOWN),
        (1, 1, "dark source", EnumFreshnessState.UNKNOWN),
    ],
)
def test_done_items_classifier_states(
    demand: int | None,
    unprojected: int | None,
    error: str | None,
    expected: EnumFreshnessState,
) -> None:
    verdict = classify_done_items_coverage(
        check_id="done_items_coverage",
        demand_done_tickets=demand,
        unprojected_tickets=unprojected,
        error=error,
    )
    assert verdict.check_id == "done_items_coverage"
    assert verdict.state == expected
