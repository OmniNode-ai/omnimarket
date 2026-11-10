# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23452: the work-ledger emit dials the broker its contract overlay declares, with no environment variable.

Before this the emit read ``KAFKA_BOOTSTRAP_SERVERS`` through the shared emit effect, so on a host whose env
file holds no broker it answered ``KeyError('KAFKA_BOOTSTRAP_SERVERS')``. Each test drives the node's
production wiring (``HandlerWorkLedgerEmit()`` with no injected emitter) over real overlay and lane files in
a scratch config root. The first test fails at the merge base: the module it patches does not exist there,
and without the patch the base raises the KeyError.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker
from omnimarket.nodes.node_work_ledger_emit_effect.container.container_work_ledger_emit_effect import (
    DeclaredBrokerPublisher,
    declared_kafka_bus,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_emit import (
    HandlerWorkLedgerEmit,
)
from omnimarket.nodes.node_work_ledger_emit_effect.models.model_work_ledger_emit_request import (
    ModelWorkLedgerEmitRequest,
)
from tests.unit.nodes.node_work_ledger_emit_effect.declared_broker_support import (
    OpenInmemoryBus,
    install_emit_lane,
    loopback_broker,
    route_declared_bus,
)

pytestmark = pytest.mark.unit

KAFKA_DEV = {"type": "kafka", "lane": "dev"}


async def test_the_emit_dials_the_declared_lane_with_no_broker_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: dict[str, str]
) -> None:
    install_emit_lane(tmp_path, monkeypatch, KAFKA_DEV)
    bus = OpenInmemoryBus()
    route_declared_bus(monkeypatch, bus)

    result = HandlerWorkLedgerEmit().handle(
        ModelWorkLedgerEmitRequest(row=rows["CLAIM"])
    )

    assert result.accepted is True
    assert result.published is True
    assert result.topic == "onex.evt.omnimarket.work-ledger-claim.v1"
    assert bus.dialed == [loopback_broker()]
    assert len(await bus.published(result.topic)) == 1


def test_a_missing_declaration_is_a_typed_refusal_naming_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: dict[str, str]
) -> None:
    install_emit_lane(tmp_path, monkeypatch, None)

    result = HandlerWorkLedgerEmit().handle(
        ModelWorkLedgerEmitRequest(row=rows["CLAIM"])
    )

    assert result.accepted is False
    assert result.published is False
    assert result.error is not None
    assert result.error.startswith("broker_not_declared: ")
    assert "declares no event_bus" in result.error
    assert "node_work_ledger_emit_effect/overlay.yaml" in result.error
    # The row was spooled before the broker was resolved, so the refusal loses nothing.
    assert len(list((tmp_path / "spool").rglob("*.json"))) >= 1


def test_the_ambient_broker_variable_never_selects_the_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: dict[str, str]
) -> None:
    install_emit_lane(tmp_path, monkeypatch, None)
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", loopback_broker())

    result = HandlerWorkLedgerEmit().handle(
        ModelWorkLedgerEmitRequest(row=rows["CLAIM"])
    )

    assert result.accepted is False
    assert (result.error or "").startswith("broker_not_declared: ")


def test_the_sasl_secret_of_the_lane_never_reaches_the_publish_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "credential-marker-never-printed"
    install_emit_lane(tmp_path, monkeypatch, KAFKA_DEV)
    binding = ModelNodeBroker(
        kind="kafka",
        lane="dev",
        bootstrap_servers=loopback_broker(),
        security_protocol="SASL_PLAINTEXT",
        sasl_mechanism="SCRAM-SHA-256",
        sasl_username="dev-principal",
        sasl_password=SecretStr(marker),
    )

    adapter = DeclaredBrokerPublisher(broker=lambda _node: binding)

    assert adapter.publish_identity == (
        f"{loopback_broker()}|SASL_PLAINTEXT|dev-principal"
    )
    assert marker not in repr(adapter.publish_identity)
    bus = declared_kafka_bus(binding)
    assert marker not in repr(bus)
    assert marker not in str(bus)


def test_a_test_process_never_dials_a_declared_broker_that_is_not_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: dict[str, str]
) -> None:
    install_emit_lane(tmp_path, monkeypatch, KAFKA_DEV, broker="lane-broker.test:19092")
    bus = OpenInmemoryBus()
    route_declared_bus(monkeypatch, bus)

    result = HandlerWorkLedgerEmit().handle(
        ModelWorkLedgerEmitRequest(row=rows["CLAIM"])
    )

    assert result.accepted is False
    assert result.published is False
    assert "ledger-test-write-guard" in (result.error or "")
    assert bus.dialed == []
