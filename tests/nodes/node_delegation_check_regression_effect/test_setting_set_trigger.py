# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The regression node's trigger: the settings audit row (a settings-store change) calls it through the real contract.

A settings-store change reaches the bus as one audit row per set, on the topic node_overlay_setting_set_effect publishes.
Before this trigger no contract, topic or schedule called the regression node; the replay command was the only way in.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring import handler_wiring
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryOutcome,
    EnumDeliveryStep,
    ModelAlarmDelivery,
    ModelLabAlarmDeliveryRequest,
    ModelLabAlarmDeliveryResult,
)
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.replay_source import (
    ReplayCheckSource,
)
from omnimarket.nodes.node_delegation_check_regression_effect.handlers.handler_setting_set_trigger import (
    HandlerCheckRegressionOnSettingSet,
)
from omnimarket.nodes.node_delegation_check_regression_effect.models.model_check_regression import (
    EnumCheckRegressionOutcome,
)
from omnimarket.nodes.node_delegation_check_regression_effect.overlay import (
    DEFAULTS_PATH,
    load_settings,
)
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_audit import (
    ModelOverlaySettingAudit,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODES = ROOT / "src" / "omnimarket" / "nodes"
FIXTURE = (
    ROOT
    / "tests"
    / "fixtures"
    / "delegation_check_regression"
    / "incident_2026_11_23.json"
)
SETTING_PATH = "local_endpoints/backends/[name=local-coder]/endpoint"
SET_AT = datetime(2026, 11, 23, 13, 46, 25, tzinfo=UTC)


def _contract() -> dict[str, Any]:
    text = (
        NODES / "node_delegation_check_regression_effect" / "contract.yaml"
    ).read_text(encoding="utf-8")
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


def _audit_topic() -> str:
    """The topic the settings-set node publishes its audit row on, read from that node's own contract."""
    setter = yaml.safe_load(
        (NODES / "node_overlay_setting_set_effect" / "contract.yaml").read_text(
            encoding="utf-8"
        )
    )
    topics = [
        t
        for t in setter["event_bus"]["publish_topics"]
        if t.endswith("overlay-setting-set.v1")
    ]
    assert len(topics) == 1
    return str(topics[0])


def _audit(
    overlay_id: str = "delegation_routing", path: str = SETTING_PATH
) -> ModelOverlaySettingAudit:
    return ModelOverlaySettingAudit(
        overlay_id=overlay_id,
        key="endpoint",
        setting_path=path,
        actor="operator",
        previous_present=True,
        previous_value="h201:8000",
        new_value="h202:8000",
        document_digest="0" * 64,
        set_at=SET_AT,
    )


class _Delivery:
    def __init__(self) -> None:
        self.requests: list[ModelLabAlarmDeliveryRequest] = []

    def handle(
        self, request: ModelLabAlarmDeliveryRequest
    ) -> ModelLabAlarmDeliveryResult:
        self.requests.append(request)
        return ModelLabAlarmDeliveryResult(
            correlation_id=request.correlation_id,
            condition=request.condition,
            outcome=EnumDeliveryOutcome.DELIVERED,
            deliveries=tuple(
                ModelAlarmDelivery(
                    key=a.key,
                    channel=EnumDeliveryStep.DELIVERED,
                    ledger=EnumDeliveryStep.DELIVERED,
                )
                for a in request.alarms
            ),
            cleared=(),
            retried=(),
            state_kept=True,
        )


def _source() -> ReplayCheckSource:
    import json

    incident = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return ReplayCheckSource.from_readings(
        incident["readings"],
        previous_through=incident["transition_window"]["green_through"],
        current_through=incident["transition_window"]["red_from"],
    )


def test_the_contract_subscribes_to_the_settings_audit_topic_the_setter_publishes() -> (
    None
):
    contract = _contract()
    topic = _audit_topic()
    assert topic in contract["event_bus"]["subscribe_topics"]
    routes = contract["handler_routing"]["handlers"]
    assert [r["topic"] for r in routes] == [topic]
    assert contract["runtime_dispatch"]["command_topic"] == topic


def test_the_trigger_handler_turns_one_audit_row_into_one_settings_change() -> None:
    delivery = _Delivery()
    handler = HandlerCheckRegressionOnSettingSet(
        settings=load_settings(DEFAULTS_PATH),
        runner=_source(),
        history=_source(),
        delivery=delivery,
        clock=lambda: SET_AT,
    )
    result = handler.handle(_audit())
    assert result.outcome is EnumCheckRegressionOutcome.ALARM
    assert {a.check_id for a in result.alarms} >= {"C14", "C29"}
    assert result.changed_keys == (SETTING_PATH,)


def test_an_audit_row_of_an_unrelated_overlay_selects_no_check() -> None:
    handler = HandlerCheckRegressionOnSettingSet(
        settings=load_settings(DEFAULTS_PATH),
        runner=_source(),
        history=_source(),
        delivery=_Delivery(),
        clock=lambda: SET_AT,
    )
    result = handler.handle(_audit(overlay_id="some_other_overlay", path="x/y"))
    assert result.outcome is EnumCheckRegressionOutcome.NO_CHECKS


def test_the_target_is_a_setting_and_a_prod_target_is_refused() -> None:
    settings = load_settings(DEFAULTS_PATH)
    assert settings.trigger_target == "dev"
    prod = settings.model_copy(update={"trigger_target": "prod"})
    handler = HandlerCheckRegressionOnSettingSet(
        settings=prod, runner=_source(), history=_source(), delivery=_Delivery()
    )
    assert handler.handle(_audit()).outcome is EnumCheckRegressionOutcome.REFUSED


async def test_the_audit_row_on_the_bus_fires_the_real_contract_and_raises_the_alarms() -> (
    None
):
    package = importlib.import_module(
        "omnimarket.nodes.node_delegation_check_regression_effect"
    )
    assert package.__file__ is not None
    manifest = discover_contracts_from_paths(
        [Path(package.__file__).parent / "contract.yaml"]
    )
    assert not manifest.errors, manifest.errors
    assert len(manifest.contracts) == 1
    delivery = _Delivery()
    seen: list[Any] = []

    class Injected(HandlerCheckRegressionOnSettingSet):
        def __init__(self) -> None:
            super().__init__(
                settings=load_settings(DEFAULTS_PATH),
                runner=_source(),
                history=_source(),
                delivery=delivery,
                clock=lambda: SET_AT,
            )

        def handle(self, request: Any) -> Any:
            result = super().handle(request)
            seen.append(result)
            return result

    real_import = handler_wiring._import_handler_class

    def import_handler(module_path: str, class_name: str) -> type:
        if class_name == "HandlerCheckRegressionOnSettingSet":
            return Injected
        return real_import(module_path, class_name)

    bus = EventBusInmemory(environment="test", group="omn26652-trigger")
    await bus.start()
    try:
        engine = MessageDispatchEngine()
        with (
            patch.object(handler_wiring, "_import_handler_class", import_handler),
            patch.object(
                handler_wiring, "_route_apply_publish_failure", new_callable=AsyncMock
            ) as route_failure,
        ):
            report = await handler_wiring.wire_from_manifest(
                manifest, engine, event_bus=bus, environment="local"
            )
            assert report.total_wired == 1, report.model_dump()
            engine.freeze()
            envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
                payload=_audit().model_dump(mode="json"),
                correlation_id=uuid4(),
                event_type="omnimarket.overlay-setting-set",
            )
            await bus.publish(
                _audit_topic(), None, envelope.model_dump_json().encode("utf-8"), None
            )
    finally:
        await bus.close()
    assert len(seen) == 1, "the audit row did not reach the handler"
    assert {a.check_id for a in seen[0].alarms} >= {"C14", "C29"}
    assert delivery.requests, "no alarm reached the delivery"
    route_failure.assert_not_awaited()


async def test_the_real_handler_class_wires_and_quarantines_nothing() -> None:
    """The runtime builds the contract's handler from its class, so the class must build with no argument (OMN-26652)."""
    package = importlib.import_module(
        "omnimarket.nodes.node_delegation_check_regression_effect"
    )
    assert package.__file__ is not None
    manifest = discover_contracts_from_paths(
        [Path(package.__file__).parent / "contract.yaml"]
    )
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="test", group="omn26652-real-class")
    await bus.start()
    try:
        report = await handler_wiring.wire_from_manifest(
            manifest, MessageDispatchEngine(), event_bus=bus, environment="local"
        )
    finally:
        await bus.close()
    assert report.total_wired == 1, report.model_dump()
    assert not [
        r.quarantined_handlers for r in report.results if r.quarantined_handlers
    ]


def test_a_handler_built_with_no_runner_fails_loudly_rather_than_reading_unread() -> (
    None
):
    handler = HandlerCheckRegressionOnSettingSet()
    with pytest.raises(RuntimeError, match="is configured"):
        handler.handle(_audit())


def test_the_completed_topic_is_declared_as_leaving_the_node_graph() -> None:
    """contract-topic-graph refuses a published topic no contract consumes unless the contract declares who does (OMN-26652)."""
    from omnimarket.validators.contract_topic_graph import parse_contract

    node = parse_contract(
        NODES / "node_delegation_check_regression_effect" / "contract.yaml",
        "omnimarket",
    )
    assert node is not None
    completed = "onex.evt.omnimarket.delegation-check-regression-completed.v1"
    assert completed in node.publish_topics
    assert completed in node.externally_consumed
