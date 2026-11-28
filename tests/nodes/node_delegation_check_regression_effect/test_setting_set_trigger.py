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
from omnimarket.models.overlay_store.model_overlay_setting_audit import (
    ModelOverlaySettingAudit,
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
    """The topic the settings-set node declares for its audit row, read from that node's own contract.

    The audit row rides the node's write; node_projection_overlay_store publishes it, so the node declares it as externally consumed.
    """
    setter = yaml.safe_load(
        (NODES / "node_overlay_setting_set_effect" / "contract.yaml").read_text(
            encoding="utf-8"
        )
    )
    topics = [
        t
        for t in setter["externally_consumed_topics"]
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


def test_the_handler_constructs_with_no_arguments_and_fails_loudly_on_use() -> None:
    """Auto-wiring builds the handler with no arguments; a missing dependency is a RuntimeError at use, never a silent skip."""
    handler = HandlerCheckRegressionOnSettingSet()
    with pytest.raises(RuntimeError, match="settings"):
        handler.handle(_audit())


@pytest.mark.parametrize("missing", ["settings", "runner", "history"])
def test_each_missing_dependency_is_named_by_the_runtime_error(missing: str) -> None:
    kwargs: dict[str, Any] = {
        "settings": load_settings(DEFAULTS_PATH),
        "runner": _source(),
        "history": _source(),
    }
    del kwargs[missing]
    handler = HandlerCheckRegressionOnSettingSet(**kwargs)
    with pytest.raises(RuntimeError, match=missing):
        handler.handle(_audit())


def test_the_contract_handler_block_names_the_canonical_handler() -> None:
    block = _contract()["handler"]
    assert block["class"] == "HandlerCheckRegression"
    assert block["module"].endswith(".handlers.handler_check_regression")


def test_the_completed_topic_is_declared_externally_consumed() -> None:
    contract = _contract()
    assert (
        "onex.evt.omnimarket.delegation-check-regression-completed.v1"
        in contract["externally_consumed_topics"]
    )


def test_the_trigger_handler_adds_no_type_ignore() -> None:
    source = (
        NODES
        / "node_delegation_check_regression_effect"
        / "handlers"
        / "handler_setting_set_trigger.py"
    ).read_text(encoding="utf-8")
    assert "type: ignore" not in source


async def test_the_real_class_wires_from_the_real_contract_and_an_audit_row_without_dependencies_fails_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    package = importlib.import_module(
        "omnimarket.nodes.node_delegation_check_regression_effect"
    )
    assert package.__file__ is not None
    manifest = discover_contracts_from_paths(
        [Path(package.__file__).parent / "contract.yaml"]
    )
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="test", group="omn26652-trigger")
    await bus.start()
    try:
        engine = MessageDispatchEngine()
        with (
            caplog.at_level("ERROR"),
            patch.object(
                handler_wiring, "_route_apply_publish_failure", new_callable=AsyncMock
            ) as route_failure,
        ):
            report = await handler_wiring.wire_from_manifest(
                manifest, engine, event_bus=bus, environment="local"
            )
            assert report.total_wired == 1, report.model_dump()
            assert not report.total_failed, report.model_dump()
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
    del route_failure
    assert any(
        "RuntimeError: HandlerCheckRegressionOnSettingSet was built without"
        in r.getMessage()
        for r in caplog.records
    ), "the missing dependencies did not fail loudly"
