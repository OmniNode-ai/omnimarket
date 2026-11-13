# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Invoke the packaged contract with a typed request (OMN-22414)."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml
from pydantic import BaseModel

from tests.nodes.node_lane_toolchain_provision_effect.test_lane_toolchain_provision import (
    NODE_INSTALL,
    TABLE_TEXT,
    FakeHost,
    overlay,
)

pytestmark = pytest.mark.unit


def test_contract_golden_chain() -> None:
    package = importlib.import_module(
        "omnimarket.nodes.node_lane_toolchain_provision_effect"
    )
    assert package.__file__ is not None
    root = Path(package.__file__).parent
    contract = yaml.safe_load((root / "contract.yaml").read_text())
    metadata = yaml.safe_load((root / "metadata.yaml").read_text())
    assert (
        contract["name"] == metadata["name"] == "node_lane_toolchain_provision_effect"
    )
    assert contract["metadata"]["related_tickets"] == ["OMN-22414"]
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
        "onex.evt.omnimarket.lane-toolchain-provision-completed.v1"
    )

    from omnimarket.nodes.node_lane_toolchain_provision_effect.handlers.adapter_host_files import (
        load_table,
    )

    handler = handler_class(
        overlay(NODE_INSTALL),
        load_table(TABLE_TEXT),
        FakeHost(),
    )
    result = handler.handle(request_class(host="lane-a.test"))
    assert isinstance(result, result_class)
    assert [s.state for s in result.steps] == ["changed", "changed", "changed"]


@pytest.mark.parametrize(
    ("command", "state"),
    [
        ("pnpm --version; true", "needs-operator"),
        ("printf '10.20.0\\n'", "unchanged"),
    ],
)
def test_contract_handler_reads_real_shell_probe(
    tmp_path: Path, command: str, state: str
) -> None:
    from omnimarket.nodes.node_lane_toolchain_provision_effect.handlers.adapter_host_files import (
        load_table,
    )

    package = importlib.import_module(
        "omnimarket.nodes.node_lane_toolchain_provision_effect"
    )
    assert package.__file__ is not None
    contract = yaml.safe_load(
        Path(package.__file__).with_name("contract.yaml").read_text()
    )
    binding = contract["handler"]
    handler_class = getattr(
        importlib.import_module(binding["module"]), binding["class"]
    )
    request_module, _, request_name = binding["input_model"].rpartition(".")
    request_class = getattr(importlib.import_module(request_module), request_name)

    # Keep the packaged repair rule but replay the recorded shell wrapper with
    # an empty PATH. The default host adapter runs /bin/sh and captures stderr;
    # the shell's final builtin masks pnpm's failure with status zero.
    table = load_table()
    spec = table.tools["pnpm"].model_copy(update={"probe": ("/bin/sh", "-c", command)})
    table = table.model_copy(
        update={"tools": {"pnpm": spec}, "hosts": {"lane-a.test": ("pnpm",)}}
    )
    deployment = overlay()
    host = deployment.hosts[0].model_copy(update={"lane_path": str(tmp_path)})
    deployment = deployment.model_copy(update={"hosts": (host,)})
    result = handler_class(deployment, table).handle(request_class(host=host.name))

    assert [(step.step, step.state) for step in result.steps] == [("tool:pnpm", state)]
    assert "exit 0" in result.steps[0].detail
    assert bool(result.operator_steps) == (state == "needs-operator")


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
        "omnimarket.nodes.node_lane_toolchain_provision_effect"
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
