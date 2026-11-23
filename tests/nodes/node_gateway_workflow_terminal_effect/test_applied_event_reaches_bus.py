# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The writer's decision event reaches the bus, not the drop log (OMN-26594).

On dev, the first delegation-failed delivery the writer received at
2026-11-23T22:01:41Z returned ``ModelGatewayTerminalApplied`` and the runtime
logged ``Auto-wiring boundary has no result applier for output-producing
dispatch ... the event would be dropped and the offset committed``. The kernel
registers a contract result applier only for a contract that declares
``event_bus.publish_topics`` and a ``published_events`` map; this contract
declared neither, only ``runtime_dispatch.terminal_events.success``, so the
decision the contract says the runtime publishes was dropped on every delivery.

These tests wire the applier the way omnibase_infra's auto-wiring does, over the
REAL contract file, and publish the handler's result through it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.enums import EnumDispatchStatus
from omnibase_infra.models.dispatch.model_dispatch_result import ModelDispatchResult
from omnibase_infra.protocols import ProtocolEventBusLike
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _select_dispatch_result_output_topic,
)
from omnibase_infra.runtime.event_bus_subcontract_wiring import (
    load_published_events_map,
)
from omnibase_infra.runtime.service_dispatch_result_applier import (
    build_contract_result_applier,
)

from omnimarket.nodes.node_gateway_workflow_terminal_effect.handlers.handler_gateway_workflow_terminal import (
    load_contract_routes,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_gateway_terminal_applied import (
    EnumGatewayTerminalOutcome,
    ModelGatewayTerminalApplied,
)

pytestmark = pytest.mark.unit

APPLIED = "onex.evt.omnimarket.gateway-workflow-terminal-applied.v1"
_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_gateway_workflow_terminal_effect"
    / "contract.yaml"
)


def _contract() -> dict[str, Any]:
    document = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _decision() -> ModelGatewayTerminalApplied:
    return ModelGatewayTerminalApplied(
        outcome=EnumGatewayTerminalOutcome.NOT_A_GATEWAY_WORKFLOW
    )


def _kernel_registers_an_applier(contract_path: Path) -> bool:
    """The kernel's gate for registering a result applier from a contract.

    omnibase_infra service_kernel (OMN-12409): a contract with no declared
    publish topic, or an empty ``published_events`` map, gets no applier.
    """
    manifest = discover_contracts_from_paths([contract_path])
    assert not manifest.errors, manifest.errors
    assert not manifest.skips, manifest.skips
    event_bus = manifest.contracts[0].event_bus
    if event_bus is None or not event_bus.publish_topics:
        return False
    return bool(load_published_events_map(contract_path))


def test_the_kernel_registers_a_result_applier_for_the_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_ACTIVE_RUNTIME_PACKAGES", raising=False)

    assert _kernel_registers_an_applier(_CONTRACT_PATH)


def test_control_the_kernel_gate_registers_a_contract_that_declares_the_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Positive control: the gate above is true for a contract that declares it."""
    monkeypatch.delenv("ONEX_ACTIVE_RUNTIME_PACKAGES", raising=False)
    contract = _contract()
    contract["event_bus"]["publish_topics"] = [APPLIED]
    contract["published_events"] = [
        {"event_type": "ModelGatewayTerminalApplied", "topic": APPLIED}
    ]
    declared = tmp_path / "contract.yaml"
    declared.write_text(yaml.safe_dump(contract), encoding="utf-8")

    assert _kernel_registers_an_applier(declared)


@pytest.mark.asyncio
async def test_the_decision_is_published_on_the_declared_terminal_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_ACTIVE_RUNTIME_PACKAGES", raising=False)
    manifest = discover_contracts_from_paths([_CONTRACT_PATH])
    assert not manifest.errors, manifest.errors
    contract = manifest.contracts[0]
    assert contract.event_bus is not None
    applier = build_contract_result_applier(
        event_bus=AsyncMock(spec=ProtocolEventBusLike),
        contract_path=_CONTRACT_PATH,
        publish_topics=contract.event_bus.publish_topics,
        output_topic=_select_dispatch_result_output_topic(contract),
        output_topic_map=load_published_events_map(_CONTRACT_PATH),
        allowed_output_topics=contract.event_bus.publish_topics,
    )
    correlation_id = uuid4()

    await applier.apply(
        ModelDispatchResult(
            status=EnumDispatchStatus.SUCCESS,
            topic=next(iter(load_contract_routes()[0])),
            started_at=datetime.now(UTC),
            correlation_id=correlation_id,
            dispatcher_id="omn26594-seam",
            output_events=[_decision()],
        ),
        correlation_id=correlation_id,
    )

    bus = applier._event_bus
    bus.publish_envelope.assert_awaited_once()
    assert str(bus.publish_envelope.await_args.kwargs["topic"]) == APPLIED


def test_the_contract_names_the_decision_topic_once_everywhere_it_must() -> None:
    contract = _contract()

    assert contract["runtime_dispatch"]["terminal_events"]["success"] == APPLIED
    assert contract["terminal_event"] == APPLIED
    assert contract["event_bus"]["publish_topics"] == [APPLIED]
    assert contract["published_events"] == [
        {
            "event_type": "ModelGatewayTerminalApplied",
            "topic": APPLIED,
            "description": contract["published_events"][0]["description"],
        }
    ]
    assert contract["published_events"][0]["description"]


def test_the_runtime_principal_may_write_the_decision_topic() -> None:
    grants = _contract()["broker_acl_grants"]
    assert len(grants) == 1
    written = {
        (grant["name"], grant["operation"])
        for grant in grants[0]["grants"]
        if grant["resource_type"] == "TOPIC" and grant["pattern"] == "LITERAL"
    }

    assert (APPLIED, "WRITE") in written
    assert (APPLIED, "DESCRIBE") in written
    # Positive control: the reader sees the grant the contract already had.
    assert (
        "onex.dlq.omnimarket.gateway-workflow-terminal-malformed.v1",
        "WRITE",
    ) in written
