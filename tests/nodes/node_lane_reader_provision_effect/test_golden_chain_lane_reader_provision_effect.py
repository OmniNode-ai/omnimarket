# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Invoke the packaged contract with a typed request (OMN-22100)."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml
from pydantic import BaseModel

from tests.nodes.node_lane_reader_provision_effect.test_lane_reader_provision import (
    TABLE_TEXT,
    host_row,
    prepared_host,
)

pytestmark = pytest.mark.unit


def test_contract_golden_chain() -> None:
    package = importlib.import_module(
        "omnimarket.nodes.node_lane_reader_provision_effect"
    )
    assert package.__file__ is not None
    root = Path(package.__file__).parent
    contract = yaml.safe_load((root / "contract.yaml").read_text())
    metadata = yaml.safe_load((root / "metadata.yaml").read_text())
    assert contract["name"] == metadata["name"] == "node_lane_reader_provision_effect"
    assert contract["metadata"]["related_tickets"] == ["OMN-22100"]
    binding = contract["handler"]
    handler_class = getattr(
        importlib.import_module(binding["module"]), binding["class"]
    )
    assert (
        contract["handler_routing"]["default_handler"]
        == binding["module"] + ":" + binding["class"]
    )

    def model(ref: str) -> type[BaseModel]:
        module, _, name = ref.rpartition(".")
        result = getattr(importlib.import_module(module), name)
        assert issubclass(result, BaseModel)
        assert result.model_config == {"frozen": True, "extra": "forbid"}
        return result

    request_class = model(binding["input_model"])
    result_class = model(binding["output_model"])
    hints = get_type_hints(handler_class.handle)
    assert hints["request"] is request_class
    assert hints["return"] is result_class
    assert contract["event_bus"]["subscribe_topics"] == [
        contract["runtime_dispatch"]["command_topic"]
    ]
    assert contract["event_bus"]["publish_topics"] == [contract["terminal_event"]]
    assert contract["terminal_event"] == (
        "onex.evt.omnimarket.lane-reader-provision-completed.v1"
    )

    from omnimarket.nodes.node_lane_reader_provision_effect.handlers.adapter_host_files import (
        load_table,
    )
    from omnimarket.nodes.node_lane_reader_provision_effect.models import (
        ModelLaneReaderProvisionOverlay,
    )

    handler = handler_class(
        ModelLaneReaderProvisionOverlay(hosts=(host_row(),)),
        load_table(TABLE_TEXT),
        prepared_host(),
    )
    result = handler.handle(request_class(host="lane-a.test"))
    assert isinstance(result, result_class)
    assert [s.state for s in result.steps] == [
        "changed",
        "changed",
        "changed",
        "unchanged",
    ]


async def test_node_auto_wires_before_host_overlay_is_loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnibase_core.container import ModelONEXContainer
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
    from omnibase_infra.topology import load_topology_profile

    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    package = importlib.import_module(
        "omnimarket.nodes.node_lane_reader_provision_effect"
    )
    assert package.__file__ is not None
    discovered = discover_contracts_from_paths(
        [Path(package.__file__).with_name("contract.yaml")]
    )
    assert not discovered.errors
    assert len(discovered.contracts) == 1
    bus = EventBusInmemory()
    try:
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=tuple(discovered.contracts)),
            MessageDispatchEngine(),
            event_bus=bus,
            environment="local",
            container=ModelONEXContainer(),
            topology=load_topology_profile("local"),
        )
    finally:
        await bus.close()

    assert not report.quarantined_handlers
    assert report.total_wired == 1
