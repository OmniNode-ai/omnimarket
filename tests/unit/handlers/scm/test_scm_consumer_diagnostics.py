# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Broker reachability must not be mistaken for SCM consumer availability."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from omnimarket.handlers.scm.handler_scm_consumer_diagnostics import (
    diagnose_scm_timeout,
    read_consumer_evidence,
)
from omnimarket.models.scm import EnumScmNode

pytestmark = pytest.mark.unit
TOPIC = "onex.cmd.omnimarket.github-repo-write-requested.v1"
PREFIX = "local.omnimarket.node_github_repo_write_effect.consume."
GROUP = PREFIX + "1.5.0.__i.runtime-effects.__t." + TOPIC


class Future:
    def __init__(self, value: Any) -> None:
        self.value = value

    def result(self, timeout: float) -> Any:
        assert 0 < timeout <= 5
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def install_admin(
    monkeypatch: pytest.MonkeyPatch,
    groups: dict[str, Any],
    *,
    errors: list[Any] | None = None,
) -> list[dict[str, Any]]:
    configs: list[dict[str, Any]] = []

    class Admin:
        def __init__(self, config: dict[str, Any]) -> None:
            configs.append(config)

        def list_consumer_groups(self, **kwargs: Any) -> Future:
            return Future(
                SimpleNamespace(
                    errors=errors or [],
                    valid=[SimpleNamespace(group_id=g) for g in groups],
                )
            )

        def describe_consumer_groups(
            self, names: list[str], **kwargs: Any
        ) -> dict[str, Future]:
            return {n: Future(groups[n]) for n in names}

    monkeypatch.setattr("confluent_kafka.admin.AdminClient", Admin)
    return configs


def description(state: str, *topics: str) -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(name=state),
        members=[
            SimpleNamespace(
                assignment=SimpleNamespace(
                    topic_partitions=[SimpleNamespace(topic=t) for t in topics]
                )
            )
        ]
        if topics
        else [],
    )


def read() -> Any:
    return read_consumer_evidence(
        {"bootstrap.servers": "declared:9092"}, PREFIX, TOPIC, TOPIC
    )


@pytest.mark.parametrize("groups", [{}, {GROUP: description("EMPTY")}])
def test_missing_or_empty_runtime_group_names_no_consumer(
    monkeypatch: pytest.MonkeyPatch, groups: dict[str, Any]
) -> None:
    install_admin(monkeypatch, groups)
    evidence = read()
    assert evidence.reason == "no_consumer_for_topic"
    assert evidence.consumer_count == 0
    assert evidence.command_topic == TOPIC


def test_live_assignment_is_no_reply_not_no_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_admin(monkeypatch, {GROUP: description("STABLE", TOPIC)})
    evidence = read()
    assert evidence.reason == "consumer_present_no_terminal"
    assert evidence.consumer_count == 1


def test_other_lane_and_audit_consumers_cannot_make_dev_covered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_admin(
        monkeypatch,
        {
            GROUP.replace("local.", "prepr1.", 1): description(
                "STABLE", "prepr1." + TOPIC
            ),
            "audit-reader": description("STABLE", TOPIC),
            GROUP: description("STABLE", "prepr1." + TOPIC),
        },
    )
    assert read().reason == "no_consumer_for_topic"


@pytest.mark.parametrize(
    ("groups", "errors"),
    [
        ({GROUP: description("PREPARING_REBALANCING")}, []),
        ({GROUP: RuntimeError("secret-value")}, []),
        ({}, [RuntimeError("secret-value")]),
    ],
)
def test_incomplete_evidence_never_claims_zero_consumers(
    monkeypatch: pytest.MonkeyPatch, groups: dict[str, Any], errors: list[Any]
) -> None:
    install_admin(monkeypatch, groups, errors=errors)
    evidence = read()
    assert evidence.reason == "consumer_evidence_unreadable"
    assert evidence.consumer_count is None
    assert "secret-value" not in evidence.model_dump_json()


def test_unknown_transport_is_explicitly_unreadable() -> None:
    evidence = asyncio.run(diagnose_scm_timeout(object(), EnumScmNode.WRITE, TOPIC))
    assert evidence.reason == "consumer_evidence_unreadable"


def test_expired_diagnostic_budget_preserves_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.handlers.scm import handler_scm_consumer_diagnostics as diagnostics

    install_admin(monkeypatch, {GROUP: description("STABLE", TOPIC)})
    times = iter([0.0, 6.0])
    monkeypatch.setattr(diagnostics, "monotonic", lambda: next(times))
    evidence = read()
    assert evidence.reason == "consumer_evidence_unreadable"
    assert evidence.consumer_count is None
    assert evidence.detail == "TimeoutError"


def test_diagnostic_uses_opened_transport_auth_and_physical_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig

    configs = install_admin(
        monkeypatch, {GROUP: description("STABLE", "tenant." + TOPIC)}
    )
    bus = SimpleNamespace(
        config=ModelKafkaEventBusConfig(
            bootstrap_servers="selected:9092",
            environment="local",
            security_protocol="SASL_PLAINTEXT",
            sasl_mechanism="SCRAM-SHA-256",
            sasl_plain_username="test-user",
            sasl_plain_password="test-password",
        ),
        resolve_bus_binding=lambda topic: SimpleNamespace(
            physical_topic="tenant." + topic
        ),
    )
    evidence = asyncio.run(diagnose_scm_timeout(bus, EnumScmNode.WRITE, TOPIC))
    assert evidence.reason == "consumer_present_no_terminal"
    assert configs[0]["bootstrap.servers"] == "selected:9092"
    assert configs[0]["sasl.username"] == "test-user"
    assert configs[0]["sasl.password"] == "test-password"
    assert "test-password" not in evidence.model_dump_json()
