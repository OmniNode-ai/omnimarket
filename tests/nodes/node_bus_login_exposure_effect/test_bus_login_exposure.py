# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_bus_login_exposure_effect: golden chain, error chain and named failing controls, proven on
both overlays (the in-memory source and the Redpanda source behind an admin stand-in), plus the
production admin wrapper against a stand-in AdminClient (OMN-22860)."""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_bus_login_exposure_effect.adapters.redpanda_bus_source import (
    ConfluentBrokerAdmin,
)
from omnimarket.nodes.node_bus_login_exposure_effect.handlers.handler_bus_login_exposure import (
    HandlerBusLoginExposure,
    load_topics,
)
from omnimarket.nodes.node_bus_login_exposure_effect.models.model_bus_login_exposure import (
    EnumBusReadFailure,
    ModelBusLoginExposureRequest,
    ModelBusLoginExposureResult,
)
from omnimarket.nodes.node_bus_login_exposure_effect.overlay import (
    NODE_NAME,
    load_overlay,
)
from omnimarket.nodes.node_bus_login_exposure_effect.runtime_bus_login_exposure import (
    main,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_bus_login_exposure_effect"
)
USER_ENV = "EXAMPLE_BUS_LOGIN_USER"
PASSWORD_ENV = "EXAMPLE_BUS_LOGIN_PASSWORD"
PASSWORD_VALUE = "example-password-value-0123456789"
ALLOWED_TOPIC = "example.evt.ci.completed.v1"
ALLOWED_PREFIX = "example.cmd.ci."
ALLOWED_GROUP_PREFIX = "example.ci.consume."
EXTRA_TOPIC = "example.evt.private.secret.v1"
EXTRA_GROUP = "example.private.consume.worker"
SETTINGS = {
    "allowed_topics": [ALLOWED_TOPIC],
    "allowed_topic_prefixes": [ALLOWED_PREFIX],
    "allowed_groups": [],
    "allowed_group_prefixes": [ALLOWED_GROUP_PREFIX],
    "max_age_seconds": 3600,
}
BACKENDS = ("inmemory", "redpanda")


@dataclass
class Scenario:
    """The view of one login, served by either backend; the defaults are a clean login."""

    topics: list[str] = field(
        default_factory=lambda: [ALLOWED_TOPIC, f"{ALLOWED_PREFIX}start.v1"]
    )
    groups: list[str] = field(default_factory=lambda: [f"{ALLOWED_GROUP_PREFIX}worker"])
    read_age_seconds: int = 60
    read_failure: EnumBusReadFailure | None = None


class FakeAdmin:
    """Stands in for ProtocolBrokerAdmin: serves the scenario, or raises what the broker raises."""

    def __init__(self, scenario: Scenario, raises: Exception | None = None) -> None:
        self._scenario = scenario
        self._raises = raises
        self.config: Mapping[str, str] = {}

    def visible_topics(self, timeout: float) -> tuple[str, ...]:
        if self._raises is not None:
            raise self._raises
        return tuple(self._scenario.topics)

    def visible_groups(self, timeout: float) -> tuple[str, ...]:
        return tuple(self._scenario.groups)


def _source(backend: str, scenario: Scenario) -> dict[str, Any]:
    if backend == "inmemory":
        fixture: dict[str, Any] = {
            "topics": scenario.topics,
            "groups": scenario.groups,
            "read_age_seconds": scenario.read_age_seconds,
        }
        if scenario.read_failure is not None:
            fixture["read_failure"] = scenario.read_failure.value
        return {"backend": "inmemory", "fixture": fixture}
    return {
        "backend": "redpanda",
        "bootstrap_servers": "broker.example.test:9092",
        "security_protocol": "SASL_PLAINTEXT",
        "sasl_mechanism": "SCRAM-SHA-512",
        "username_env": USER_ENV,
        "password_env": PASSWORD_ENV,
        "timeout_seconds": 5,
    }


def _handler(
    backend: str,
    scenario: Scenario,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    environ: dict[str, str] | None = None,
    raises: Exception | None = None,
) -> tuple[HandlerBusLoginExposure, list[FakeAdmin]]:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        NODE_NAME,
        {"settings": SETTINGS, "source": _source(backend, scenario)},
    )
    made: list[FakeAdmin] = []

    def factory(config: Mapping[str, str]) -> FakeAdmin:
        admin = FakeAdmin(scenario, raises)
        admin.config = dict(config)
        made.append(admin)
        return admin

    env = {USER_ENV: "example-login", PASSWORD_ENV: PASSWORD_VALUE}
    if environ is not None:
        env = environ
    return (
        HandlerBusLoginExposure(
            overlay=load_overlay(), admin_factory=factory, environ=env
        ),
        made,
    )


def _probe(handler: HandlerBusLoginExposure) -> ModelBusLoginExposureResult:
    return handler.handle(ModelBusLoginExposureRequest(correlation_id="c-1", now=NOW))


@pytest.mark.parametrize("backend", BACKENDS)
def test_golden_clean_login_reads_working_with_its_visible_topics_and_groups(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, _ = _handler(backend, Scenario(), tmp_path, monkeypatch)
    result = _probe(handler)
    reading = result.reading
    assert result.backend == backend
    assert reading.verdict is EnumReadingVerdict.WORKING
    assert reading.visible_topics == tuple(
        sorted([ALLOWED_TOPIC, f"{ALLOWED_PREFIX}start.v1"])
    )
    assert reading.visible_groups == (f"{ALLOWED_GROUP_PREFIX}worker",)
    assert reading.exposed_topics == ()
    assert reading.exposed_groups == ()
    topic, payload = handler.event_for(result)
    assert topic == load_topics()["probe_completed"]
    assert payload["reading"]["verdict"] == "WORKING"


@pytest.mark.parametrize("backend", BACKENDS)
def test_injected_break_one_extra_topic_is_reported_as_an_exposure(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean, _ = _handler(backend, Scenario(), tmp_path, monkeypatch)
    assert _probe(clean).reading.verdict is EnumReadingVerdict.WORKING
    broken = Scenario(topics=[*Scenario().topics, EXTRA_TOPIC])
    handler, _ = _handler(backend, broken, tmp_path, monkeypatch)
    reading = _probe(handler).reading
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert reading.exposed_topics == (EXTRA_TOPIC,)
    assert reading.exposed_groups == ()
    assert EXTRA_TOPIC in reading.visible_topics


@pytest.mark.parametrize("backend", BACKENDS)
def test_injected_break_one_extra_group_is_reported_as_an_exposure(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = Scenario(groups=[*Scenario().groups, EXTRA_GROUP])
    handler, _ = _handler(backend, broken, tmp_path, monkeypatch)
    reading = _probe(handler).reading
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert reading.exposed_groups == (EXTRA_GROUP,)
    assert reading.exposed_topics == ()


@pytest.mark.parametrize("backend", BACKENDS)
def test_injected_break_a_topic_that_only_shares_the_allowed_name_start_is_exposed(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    near = Scenario(
        topics=[ALLOWED_TOPIC, f"{ALLOWED_TOPIC}.shadow", "example.cmd.cix"]
    )
    handler, _ = _handler(backend, near, tmp_path, monkeypatch)
    reading = _probe(handler).reading
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert reading.exposed_topics == ("example.cmd.cix", f"{ALLOWED_TOPIC}.shadow")


def test_error_chain_a_stale_inmemory_read_reads_unknown_and_ends_in_the_failure_topic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, _ = _handler(
        "inmemory", Scenario(read_age_seconds=7200), tmp_path, monkeypatch
    )
    result = _probe(handler)
    assert result.reading.verdict is EnumReadingVerdict.UNKNOWN
    assert result.reading.stale is True
    assert handler.event_for(result)[0] == load_topics()["probe_failed"]


def test_error_chain_an_unreadable_inmemory_view_reads_unknown_never_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = Scenario(
        topics=[], groups=[], read_failure=EnumBusReadFailure.DESCRIBE_DENIED
    )
    handler, _ = _handler("inmemory", scenario, tmp_path, monkeypatch)
    result = _probe(handler)
    assert result.reading.verdict is EnumReadingVerdict.UNKNOWN
    assert result.reading.read_failure is EnumBusReadFailure.DESCRIBE_DENIED
    assert handler.event_for(result)[0] == load_topics()["probe_failed"]


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (
            RuntimeError("KafkaError AUTHORIZATION_FAILED"),
            EnumBusReadFailure.DESCRIBE_DENIED,
        ),
        (
            RuntimeError("SASL authentication failed"),
            EnumBusReadFailure.DESCRIBE_DENIED,
        ),
        (TimeoutError("timed out"), EnumBusReadFailure.UNREADABLE),
    ],
)
def test_error_chain_a_broker_error_reads_unknown_with_its_typed_failure(
    raised: Exception,
    expected: EnumBusReadFailure,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler, _ = _handler("redpanda", Scenario(), tmp_path, monkeypatch, raises=raised)
    result = _probe(handler)
    assert result.reading.verdict is EnumReadingVerdict.UNKNOWN
    assert result.reading.read_failure is expected
    assert result.reading.visible_topics == ()
    assert handler.event_for(result)[0] == load_topics()["probe_failed"]


def test_error_chain_missing_credentials_read_unknown_and_no_admin_is_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, made = _handler("redpanda", Scenario(), tmp_path, monkeypatch, environ={})
    result = _probe(handler)
    assert result.reading.verdict is EnumReadingVerdict.UNKNOWN
    assert result.reading.read_failure is EnumBusReadFailure.CREDENTIALS_MISSING
    assert made == []


def test_golden_redpanda_login_reaches_the_admin_and_never_appears_in_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, made = _handler("redpanda", Scenario(), tmp_path, monkeypatch)
    result = _probe(handler)
    assert len(made) == 1
    assert made[0].config["sasl.password"] == PASSWORD_VALUE
    assert made[0].config["sasl.username"] == "example-login"
    assert made[0].config["bootstrap.servers"] == "broker.example.test:9092"
    assert PASSWORD_VALUE not in json.dumps(handler.event_for(result)[1])


def test_overlay_absent_key_refuses_the_load_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = {k: v for k, v in SETTINGS.items() if k != "max_age_seconds"}
    install_node_overlay(
        monkeypatch,
        tmp_path,
        NODE_NAME,
        {"settings": broken, "source": _source("inmemory", Scenario())},
    )
    with pytest.raises(NodeOverlayError, match="max_age_seconds"):
        load_overlay()


@pytest.mark.parametrize(
    "example", ["overlay.example.yaml", "overlay.redpanda.example.yaml"]
)
def test_shipped_examples_load_through_the_overlay_reader(
    example: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fields = yaml.safe_load((NODE_DIR / example).read_text(encoding="utf-8"))
    install_node_overlay(monkeypatch, tmp_path, NODE_NAME, fields)
    overlay = load_overlay()
    assert overlay.source.backend == (
        "inmemory" if "redpanda" not in example else "redpanda"
    )


@pytest.mark.parametrize(
    ("scenario", "code"),
    [
        (Scenario(), 0),
        (Scenario(topics=[*Scenario().topics, EXTRA_TOPIC]), 1),
        (Scenario(read_age_seconds=999999), 2),
    ],
)
def test_golden_cli_exit_code_follows_the_verdict_on_the_inmemory_overlay(
    scenario: Scenario,
    code: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        NODE_NAME,
        {"settings": SETTINGS, "source": _source("inmemory", scenario)},
    )
    assert main([]) == code
    printed = json.loads(capsys.readouterr().out)
    assert printed["result"]["backend"] == "inmemory"


def test_golden_production_admin_wrapper_maps_the_confluent_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Future:
        def __init__(self, value: object) -> None:
            self._value = value

        def result(self, timeout: float) -> object:
            return self._value

    class _AdminClient:
        def __init__(self, config: dict[str, str]) -> None:
            self.config = config

        def list_topics(self, timeout: float) -> object:
            return types.SimpleNamespace(
                topics={"b.topic": object(), "a.topic": object()}
            )

        def list_consumer_groups(self, request_timeout: float) -> _Future:
            valid = [
                types.SimpleNamespace(group_id="g2"),
                types.SimpleNamespace(group_id="g1"),
            ]
            return _Future(types.SimpleNamespace(valid=valid, errors=[]))

    fake_module = types.ModuleType("confluent_kafka.admin")
    vars(fake_module)["AdminClient"] = _AdminClient
    monkeypatch.setitem(sys.modules, "confluent_kafka.admin", fake_module)
    admin = ConfluentBrokerAdmin({"bootstrap.servers": "broker.example.test:9092"})
    assert admin.visible_topics(1.0) == ("a.topic", "b.topic")
    assert admin.visible_groups(1.0) == ("g1", "g2")


def test_error_chain_production_admin_wrapper_refuses_an_incomplete_group_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Future:
        def result(self, timeout: float) -> object:
            return types.SimpleNamespace(valid=[], errors=[object()])

    class _AdminClient:
        def __init__(self, config: dict[str, str]) -> None:
            pass

        def list_consumer_groups(self, request_timeout: float) -> _Future:
            return _Future()

    fake_module = types.ModuleType("confluent_kafka.admin")
    vars(fake_module)["AdminClient"] = _AdminClient
    monkeypatch.setitem(sys.modules, "confluent_kafka.admin", fake_module)
    admin = ConfluentBrokerAdmin({})
    with pytest.raises(RuntimeError, match="incomplete"):
        admin.visible_groups(1.0)


def test_contract_declares_the_probe_topics_on_the_canonical_namespaces() -> None:
    topics = load_topics()
    assert (
        topics["probe_requested"]
        == "onex.cmd.omnimarket.bus-login-exposure-probe-requested.v1"
    )
    assert (
        topics["probe_completed"] == "onex.evt.omnimarket.bus-login-exposure-probed.v1"
    )
    assert (
        topics["probe_failed"]
        == "onex.evt.omnimarket.bus-login-exposure-probe-failed.v1"
    )
