# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19513: demand-aware freshness, assembly, delivery and read boundaries."""

from __future__ import annotations

import ast
import importlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
import yaml
from pydantic import SecretStr, ValidationError

from omnimarket.nodes.node_projection_freshness_alert_effect.handlers.classify_freshness import (
    classify_freshness,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.handlers.freshness_reader import (
    PostgresFreshnessReader,
    validate_identifier,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.handlers.handler_projection_freshness_alert import (
    HandlerProjectionFreshnessAlert,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.handlers.load_freshness_config import (
    load_freshness_config,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.models import (
    EnumFreshnessState,
    FreshnessPolicyError,
    ModelFreshnessCheck,
    ModelFreshnessEvaluation,
    ModelFreshnessObservation,
    ModelFreshnessSlackCommand,
    ModelFreshnessTrigger,
)
from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (
    ModelSlackPublish,
)

pytestmark = pytest.mark.unit
NODE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_freshness_alert_effect"
)
CONTRACT_PATH = NODE_ROOT / "contract.yaml"
NOW = datetime(2026, 10, 17, 18, tzinfo=UTC)
CORRELATION = UUID("8ea39bab-e7bb-4178-917e-749fbaf7aee4")
HANDLER_MODULE = importlib.import_module(HandlerProjectionFreshnessAlert.__module__)


class _Clock:
    def __init__(self) -> None:
        self.value = NOW.timestamp()

    def __call__(self) -> float:
        return self.value


class _Reader:
    def __init__(
        self, *, age: int | None = 1200, active: int = 2, error: str | None = None
    ) -> None:
        self.observation: ModelFreshnessObservation | None = ModelFreshnessObservation(
            supply_newest_at=NOW - timedelta(seconds=age) if age is not None else None,
            demand_distinct_active=active,
        )
        self.error = error
        self.calls: list[ModelFreshnessCheck] = []

    def read(self, check: ModelFreshnessCheck) -> ModelFreshnessObservation:
        self.calls.append(check)
        if self.error:
            raise RuntimeError(self.error)
        return self.observation


class _Publisher:
    def __init__(self, error: str | None = None) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self.error = error

    def __call__(self, topic: str, payload: bytes) -> None:
        if self.error:
            raise RuntimeError(self.error)
        self.calls.append((topic, payload))


@pytest.fixture(autouse=True)
def _secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        HANDLER_MODULE,
        "resolve_api_key_loop_safe",
        lambda _ref, **_kw: SecretStr("C0TESTCHANNEL"),
    )


def _contract_copy(tmp_path: Path, raw: dict[str, Any]) -> Path:
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("age", "active", "state", "expected_age"),
    [
        (1200, 0, EnumFreshnessState.IDLE, 1200),
        (100, 1, EnumFreshnessState.FRESH, 100),
        (900, 1, EnumFreshnessState.FRESH, 900),
        (901, 1, EnumFreshnessState.STALE, 901),
        (None, 1, EnumFreshnessState.STALE, None),
        (None, 0, EnumFreshnessState.IDLE, None),
        (-60, 1, EnumFreshnessState.FRESH, 0),
    ],
)
def test_classifier_states(
    age: int | None, active: int, state: EnumFreshnessState, expected_age: int | None
) -> None:
    verdict = classify_freshness(
        check_id="test",
        now=NOW,
        supply_newest_at=NOW - timedelta(seconds=age) if age is not None else None,
        demand_distinct_active=active,
        max_age_seconds=900,
        min_distinct=1,
    )
    assert verdict.state is state
    assert verdict.age_seconds == expected_age
    assert verdict.error is None


@pytest.mark.parametrize("active", [0, 1])
def test_classifier_read_error_is_unknown_even_without_demand(active: int) -> None:
    verdict = classify_freshness(
        check_id="test",
        now=NOW,
        supply_newest_at=NOW,
        demand_distinct_active=active,
        max_age_seconds=900,
        min_distinct=1,
        error="dark source",
    )
    assert verdict.state is EnumFreshnessState.UNKNOWN
    assert verdict.error == "dark source"


def test_demand_minimum_is_respected() -> None:
    verdict = classify_freshness(
        check_id="test",
        now=NOW,
        supply_newest_at=None,
        demand_distinct_active=1,
        max_age_seconds=900,
        min_distinct=2,
    )
    assert verdict.state is EnumFreshnessState.IDLE


def test_stale_publishes_canonical_slack_command() -> None:
    publisher = _Publisher()
    reader = _Reader()
    handler = HandlerProjectionFreshnessAlert(
        reader=reader, event_publisher=publisher, clock=_Clock()
    )
    result = handler.handle(ModelFreshnessTrigger(correlation_id=CORRELATION))
    policy, _, checks = load_freshness_config(CONTRACT_PATH)
    assert result.evaluated
    assert result.verdicts[0].state is EnumFreshnessState.STALE
    assert len(publisher.calls) == 1
    topic, payload = publisher.calls[0]
    raw = yaml.safe_load(CONTRACT_PATH.read_text())
    assert topic == next(
        t for t in raw["event_bus"]["publish_topics"] if "slack-publish" in t
    )
    command = ModelSlackPublish.model_validate_json(payload)
    mirror = ModelFreshnessSlackCommand.model_validate_json(payload)
    assert command.model_dump() == mirror.model_dump()
    assert set(ModelSlackPublish.model_fields) == set(
        ModelFreshnessSlackCommand.model_fields
    )
    assert (
        command.idempotency_key
        == f"projection-freshness|claude_hook_events|STALE|{int(NOW.timestamp()) // policy.renotify_after_seconds}"
    )
    assert command.text == (
        f":rotating_light: *STALE PROJECTION* — `{checks[0].supply.relation}` newest row "
        f"{reader.observation.supply_newest_at.isoformat()} (20.0 min old, limit 15 min) "
        f"while 2 distinct `actor_id` wrote `{checks[0].demand.relation}` in the last 15 min\n"
        f"correlation_id={CORRELATION}"
    )
    assert command.channel == "C0TESTCHANNEL"
    assert result.deliveries[0].published
    assert result.deliveries[0].error is None
    assert (
        ModelFreshnessEvaluation.model_validate_json(result.model_dump_json()) == result
    )


@pytest.mark.parametrize("age", [901, 9632, None])
def test_hook_alarm_survives_work_events_projection_outage(age: int | None) -> None:
    """A shared runtime outage must not turn stale hooks into an IDLE verdict."""
    publisher = _Publisher()
    result = HandlerProjectionFreshnessAlert(
        reader=_Reader(age=age, active=0),
        event_publisher=publisher,
        clock=_Clock(),
    ).handle(ModelFreshnessTrigger())
    assert result.verdicts[0].state is EnumFreshnessState.STALE
    assert len(publisher.calls) == 1
    assert result.deliveries[0].published


@pytest.mark.parametrize("age", [0, 900])
def test_hook_alarm_keeps_fresh_rows_quiet_without_demand(age: int) -> None:
    publisher = _Publisher()
    result = HandlerProjectionFreshnessAlert(
        reader=_Reader(age=age, active=0),
        event_publisher=publisher,
        clock=_Clock(),
    ).handle(ModelFreshnessTrigger())
    assert result.verdicts[0].state is EnumFreshnessState.FRESH
    assert publisher.calls == []


@pytest.mark.parametrize(
    ("age", "active", "state"),
    [
        (100, 1, EnumFreshnessState.FRESH),
        (1200, 0, EnumFreshnessState.IDLE),
        (None, 0, EnumFreshnessState.IDLE),
    ],
)
def test_fresh_and_idle_do_not_publish(
    age: int | None, active: int, state: EnumFreshnessState, tmp_path: Path
) -> None:
    # Activity gating remains available for checks whose demand is independent.
    raw = yaml.safe_load(CONTRACT_PATH.read_text())
    raw["freshness_checks"][0]["demand"]["min_distinct"] = 1
    publisher = _Publisher()
    result = HandlerProjectionFreshnessAlert(
        reader=_Reader(age=age, active=active),
        event_publisher=publisher,
        clock=_Clock(),
        contract_path=_contract_copy(tmp_path, raw),
    ).handle(ModelFreshnessTrigger())
    assert result.verdicts[0].state is state
    assert result.deliveries == ()
    assert publisher.calls == []


def test_absent_supply_alerts_without_inventing_an_age() -> None:
    publisher = _Publisher()
    result = HandlerProjectionFreshnessAlert(
        reader=_Reader(age=None), event_publisher=publisher, clock=_Clock()
    ).handle(ModelFreshnessTrigger())
    assert result.verdicts[0].state is EnumFreshnessState.STALE
    assert result.verdicts[0].age_seconds is None
    assert (
        "newest row none (unknown min old" in json.loads(publisher.calls[0][1])["text"]
    )


def test_two_ticks_in_one_interval_read_once() -> None:
    clock = _Clock()
    reader = _Reader()
    handler = HandlerProjectionFreshnessAlert(reader=reader, clock=clock)
    assert handler.handle(ModelFreshnessTrigger()).evaluated
    clock.value += 1
    second = handler.handle(ModelFreshnessTrigger())
    assert not second.evaluated
    assert second.verdicts == second.deliveries == ()
    assert len(reader.calls) == 1
    clock.value += second.check_interval_seconds
    assert handler.handle(ModelFreshnessTrigger()).evaluated
    assert len(reader.calls) == 2


@pytest.mark.parametrize("active", [0, 1])
def test_reader_failure_is_unknown(
    caplog: pytest.LogCaptureFixture, active: int
) -> None:
    publisher = _Publisher()
    with caplog.at_level(logging.ERROR):
        result = HandlerProjectionFreshnessAlert(
            reader=_Reader(error="database dark", active=active),
            event_publisher=publisher,
            clock=_Clock(),
        ).handle(ModelFreshnessTrigger())
    assert result.verdicts[0].state is EnumFreshnessState.UNKNOWN
    assert "database dark" in (result.verdicts[0].error or "")
    assert "database dark" in caplog.text
    assert publisher.calls == []
    assert result.deliveries == ()


@pytest.mark.parametrize(
    "value", [None, "bad timestamp", datetime(2026, 10, 17), True, -1]
)
def test_unusable_reader_value_is_unknown(value: object) -> None:
    reader = _Reader()
    if isinstance(value, (bool, int)):
        reader.observation = ModelFreshnessObservation.model_construct(
            supply_newest_at=NOW, demand_distinct_active=value
        )
    elif value is None:
        reader.observation = None
    else:
        reader.observation = ModelFreshnessObservation.model_construct(
            supply_newest_at=value, demand_distinct_active=1
        )
    result = HandlerProjectionFreshnessAlert(reader=reader, clock=_Clock()).handle(
        ModelFreshnessTrigger()
    )
    assert result.verdicts[0].state is EnumFreshnessState.UNKNOWN
    assert result.verdicts[0].error
    assert result.deliveries == ()


@pytest.mark.parametrize("publisher", [None, _Publisher(error="bus down")])
def test_failed_publication_is_recorded(
    publisher: _Publisher | None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR):
        result = HandlerProjectionFreshnessAlert(
            reader=_Reader(), event_publisher=publisher, clock=_Clock()
        ).handle(ModelFreshnessTrigger())
    delivery = result.deliveries[0]
    assert not delivery.published
    assert delivery.error
    assert ("bus down" if publisher else "event_publisher") in delivery.error
    assert delivery.error in caplog.text


def test_unresolved_channel_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        HANDLER_MODULE, "resolve_api_key_loop_safe", lambda *_a, **_kw: None
    )
    publisher = _Publisher()
    result = HandlerProjectionFreshnessAlert(
        reader=_Reader(), event_publisher=publisher, clock=_Clock()
    ).handle(ModelFreshnessTrigger())
    assert not result.deliveries[0].published
    assert "resolved to None" in result.deliveries[0].error
    assert publisher.calls == []


def test_missing_dsn_is_lazy_unknown_and_can_recover(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _, source, _ = load_freshness_config(CONTRACT_PATH)
    monkeypatch.delenv(source.dsn_env, raising=False)
    clock = _Clock()
    handler = HandlerProjectionFreshnessAlert(clock=clock)
    with caplog.at_level(logging.ERROR):
        result = handler.handle(ModelFreshnessTrigger())
    assert result.verdicts[0].state is EnumFreshnessState.UNKNOWN
    assert source.dsn_env in (result.verdicts[0].error or "")
    assert source.dsn_env in caplog.text
    monkeypatch.setenv(source.dsn_env, "test-dsn")
    monkeypatch.setattr(
        HANDLER_MODULE, "PostgresFreshnessReader", lambda **_kw: _Reader(age=0)
    )
    clock.value += result.check_interval_seconds
    assert (
        handler.handle(ModelFreshnessTrigger()).verdicts[0].state
        is EnumFreshnessState.FRESH
    )


def test_contract_thresholds_change_behaviour(tmp_path: Path) -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text())
    policy, _, checks = load_freshness_config(CONTRACT_PATH)
    raw["freshness_policy"]["check_interval_seconds"] = (
        policy.check_interval_seconds * 4
    )
    raw["freshness_checks"][0]["max_age_seconds"] = checks[0].max_age_seconds * 2
    path = _contract_copy(tmp_path, raw)
    clock = _Clock()
    original = HandlerProjectionFreshnessAlert(reader=_Reader(), clock=clock)
    modified = HandlerProjectionFreshnessAlert(
        reader=_Reader(), clock=clock, contract_path=path
    )
    assert (
        original.handle(ModelFreshnessTrigger()).verdicts[0].state
        is EnumFreshnessState.STALE
    )
    assert (
        modified.handle(ModelFreshnessTrigger()).verdicts[0].state
        is EnumFreshnessState.FRESH
    )
    clock.value += policy.check_interval_seconds
    assert original.handle(ModelFreshnessTrigger()).evaluated
    assert not modified.handle(ModelFreshnessTrigger()).evaluated


@pytest.mark.parametrize(
    "block", ["freshness_policy", "reads_source", "freshness_checks"]
)
@pytest.mark.parametrize("bad_value", [None, {}, [], "malformed"])
def test_missing_or_malformed_contract_fails_closed(
    tmp_path: Path, block: str, bad_value: object
) -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text())
    raw[block] = bad_value
    with pytest.raises(FreshnessPolicyError):
        HandlerProjectionFreshnessAlert(
            reader=_Reader(), contract_path=_contract_copy(tmp_path, raw)
        )
    raw.pop(block)
    with pytest.raises(FreshnessPolicyError, match=block):
        HandlerProjectionFreshnessAlert(
            reader=_Reader(), contract_path=_contract_copy(tmp_path, raw)
        )


def test_no_threshold_literal_lives_in_python() -> None:
    fields = {
        "check_interval_seconds",
        "renotify_after_seconds",
        "max_age_seconds",
        "window_seconds",
        "min_distinct",
    }
    offenders: list[str] = []
    for path in NODE_ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            targets: list[str] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets, value = [node.target.id], node.value
            elif isinstance(node, ast.keyword) and node.arg:
                targets, value = [node.arg], node.value
            if (
                isinstance(value, ast.Constant)
                and isinstance(value.value, (int, float))
                and fields.intersection(targets)
            ):
                offenders.append(f"{path.name}:{node.lineno}")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defaults = [
                    *zip(
                        node.args.args[-len(node.args.defaults) :],
                        node.args.defaults,
                        strict=False,
                    ),
                    *zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True),
                ]
                for arg, default in defaults:
                    if (
                        arg.arg in fields
                        and isinstance(default, ast.Constant)
                        and isinstance(default.value, (int, float))
                    ):
                        offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"thresholds must be contract data: {offenders}"


@pytest.mark.parametrize(
    "identifier", ["a.b; drop table x", "a.b.c", "UPPER", "a--", "a\n", "a b"]
)
def test_identifier_validation_rejects_sql_fragments(identifier: str) -> None:
    with pytest.raises(FreshnessPolicyError):
        validate_identifier(identifier)


@pytest.mark.parametrize(
    ("part", "field"),
    [
        ("supply", "relation"),
        ("supply", "timestamp_column"),
        ("demand", "relation"),
        ("demand", "timestamp_column"),
        ("demand", "distinct_column"),
    ],
)
def test_contract_identifiers_are_validated(
    tmp_path: Path, part: str, field: str
) -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text())
    raw["freshness_checks"][0][part][field] = "a.b; drop table x"
    with pytest.raises(FreshnessPolicyError):
        load_freshness_config(_contract_copy(tmp_path, raw))


def test_postgres_reader_binds_cutoff_reuses_connection_and_closes_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = importlib.import_module("omnimarket.projection.postgres_read_database")
    connect = MagicMock()
    connection = connect.return_value
    connection.closed = 0
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [(NOW,), (2,), (None,), (0,)]
    monkeypatch.setattr(boundary, "connect_read_only", connect)
    _, _, checks = load_freshness_config(CONTRACT_PATH)
    check = checks[0]
    reader = PostgresFreshnessReader(dsn="test-dsn", now=lambda: NOW)
    connect.assert_not_called()
    assert reader.read(check).supply_newest_at == NOW
    calls = cursor.execute.call_args_list
    assert calls[0].args == (
        f"SELECT max({check.supply.timestamp_column}) FROM {check.supply.relation}",
    )
    assert calls[1].args == (
        f"SELECT count(DISTINCT {check.demand.distinct_column}) FROM {check.demand.relation} WHERE {check.demand.timestamp_column} > %s",
        (NOW - timedelta(seconds=check.demand.window_seconds),),
    )
    assert reader.read(check).demand_distinct_active == 0
    connect.assert_called_once_with("test-dsn")
    cursor.execute.side_effect = RuntimeError("connection lost")
    with pytest.raises(RuntimeError, match="connection lost"):
        reader.read(check)
    connection.close.assert_called_once()
    cursor.execute.side_effect = None
    cursor.fetchone.side_effect = [(NOW,), (1,)]
    reader.read(check)
    assert connect.call_count == 2


def test_postgres_unusable_results_close_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = importlib.import_module("omnimarket.projection.postgres_read_database")
    connection = MagicMock()
    connection.closed = 0
    connection.cursor.return_value.__enter__.return_value.fetchone.side_effect = [
        (NOW,),
        (None,),
    ]
    monkeypatch.setattr(boundary, "connect_read_only", lambda _dsn: connection)
    with pytest.raises(ValidationError):
        PostgresFreshnessReader(dsn="test-dsn", now=lambda: NOW).read(
            load_freshness_config(CONTRACT_PATH)[2][0]
        )
    connection.close.assert_called_once()


def test_heartbeat_trigger_is_the_declared_all_optional_input() -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text())
    module, name = raw["input_model"].rsplit(".", 1)
    declared = getattr(importlib.import_module(module), name)
    assert declared is ModelFreshnessTrigger
    assert declared.model_validate({"uptime": 12, "flow_window": {}}) == declared()
    assert all(not field.is_required() for field in declared.model_fields.values())


def test_checks_are_generic_and_one_dark_source_does_not_abort_the_rest(
    tmp_path: Path,
) -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text())
    second = dict(raw["freshness_checks"][0], check_id="another_projection")
    raw["freshness_checks"].append(second)
    reader = MagicMock()
    reader.read.side_effect = [RuntimeError("first source dark"), _Reader().observation]
    publisher = _Publisher()
    result = HandlerProjectionFreshnessAlert(
        reader=reader,
        event_publisher=publisher,
        clock=_Clock(),
        contract_path=_contract_copy(tmp_path, raw),
    ).handle(ModelFreshnessTrigger())
    assert [v.state for v in result.verdicts] == [
        EnumFreshnessState.UNKNOWN,
        EnumFreshnessState.STALE,
    ]
    assert reader.read.call_count == 2
    assert result.deliveries[0].check_id == "another_projection"
    assert len(publisher.calls) == 1


def test_renotify_key_uses_the_declared_period(tmp_path: Path) -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text())
    policy, _, _ = load_freshness_config(CONTRACT_PATH)
    period = policy.renotify_after_seconds * 2
    raw["freshness_policy"]["renotify_after_seconds"] = period
    clock = _Clock()
    clock.value = int(clock.value) // period * period
    handler = HandlerProjectionFreshnessAlert(
        reader=_Reader(),
        clock=clock,
        contract_path=_contract_copy(tmp_path, raw),
    )
    first = handler.handle(ModelFreshnessTrigger()).deliveries[0].idempotency_key
    clock.value += policy.check_interval_seconds
    assert (
        handler.handle(ModelFreshnessTrigger()).deliveries[0].idempotency_key == first
    )
    clock.value += period
    next_key = handler.handle(ModelFreshnessTrigger()).deliveries[0].idempotency_key
    assert next_key != first
    assert next_key.endswith(f"|{int(clock.value) // period}")
