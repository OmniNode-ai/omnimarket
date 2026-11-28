# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime builds the tools/list handler from the node contract alone (OMN-15920).

Two defects were read on dev: ``onex node node_mcp_server_effect`` exited 1 because the
handler needed a registry argument the runtime never injects, and a call answered
``{"tools":[]}`` because the overlay allow list was empty. The contract now declares the
registry dependency, the factory that builds it and the bootstrap overlay.
"""

from __future__ import annotations

import importlib
import importlib.resources
from collections.abc import Sequence
from types import SimpleNamespace

import pytest
import yaml

from omnimarket.handlers.container.container_overlay_dependencies import (
    packaged_contract_value,
    packaged_factory,
)
from omnimarket.nodes.node_mcp_server_effect.handlers.handler_mcp_tools_list import (
    HandlerMcpToolsList,
)
from omnimarket.nodes.node_mcp_server_effect.models.model_mcp_tools_list import (
    EnumMcpRegistrationState,
    ModelMcpRegistryRow,
    ModelMcpToolEntry,
    ModelMcpToolsListRequest,
)
from omnimarket.nodes.node_mcp_server_effect.models.refusal import (
    EnumMcpServerRefusal,
    McpServerRefusalError,
)

PACKAGE = "omnimarket.nodes.node_mcp_server_effect"
DSN_ENV_KEY = ("config", "projection_dsn_env")


def _tool(name: str) -> ModelMcpToolEntry:
    return ModelMcpToolEntry(
        tool_name=name,
        description=f"{name} tool",
        timeout_seconds=30,
        input_model="pkg.models.In",
        output_model="pkg.models.Out",
        command_topic="onex.cmd.test.x.v1",
        completed_topic="onex.evt.test.x-completed.v1",
        failed_topic="onex.evt.test.x-failed.v1",
    )


class _Reader:
    def __init__(self, rows: Sequence[ModelMcpRegistryRow]) -> None:
        self._rows = rows

    def read_registrations(self) -> Sequence[ModelMcpRegistryRow]:
        return self._rows


def test_handler_builds_with_no_argument_as_the_runtime_does() -> None:
    # RED at origin/dev: TypeError, __init__() missing 'registry'.
    assert HandlerMcpToolsList() is not None


def test_contract_declares_the_registry_dependency_the_handler_takes() -> None:
    contract = yaml.safe_load(
        importlib.resources.files(PACKAGE).joinpath("contract.yaml").read_text()
    )
    deps = {d["name"]: d for d in contract["dependencies"]}
    assert "registry" in deps
    dep = deps["registry"]
    assert dep["dependency_type"] == "protocol"
    protocol = getattr(importlib.import_module(dep["module"]), dep["class_name"])
    assert protocol.__name__ == "ProtocolMcpRegistryReader"


def test_registry_factory_is_declared_in_the_contract_and_builds_a_reader() -> None:
    factory = packaged_factory(PACKAGE, "config", "registry_factory")
    reader = factory(lambda: [])
    assert reader.read_registrations() == ()


def test_unconfigured_projection_source_is_a_named_refusal_not_a_crash() -> None:
    handler = HandlerMcpToolsList(
        registry=packaged_factory(PACKAGE, "config", "registry_factory")(None)
    )
    with pytest.raises(McpServerRefusalError) as raised:
        handler.handle(ModelMcpToolsListRequest())
    assert raised.value.refusal is EnumMcpServerRefusal.REGISTRY_UNREADABLE


def test_bootstrap_overlay_names_the_delegate_tool() -> None:
    # RED at origin/dev: the overlay allow list was empty, so every call answered no tool.
    allowed = packaged_contract_value(PACKAGE, "config", "overlay", "allowed_tools")
    assert allowed == ["delegate"]


def test_request_without_overlay_uses_the_bootstrap_overlay() -> None:
    row = ModelMcpRegistryRow(
        node_name="node_delegate_skill_orchestrator",
        state=EnumMcpRegistrationState.ACTIVE,
        expose=True,
        tools=(_tool("delegate"), _tool("other")),
    )
    result = HandlerMcpToolsList(_Reader([row])).handle(ModelMcpToolsListRequest())
    assert [t.tool_name for t in result.tools] == ["delegate"]


def test_explicit_empty_overlay_still_denies_everything() -> None:
    row = ModelMcpRegistryRow(
        node_name="n",
        state=EnumMcpRegistrationState.ACTIVE,
        expose=True,
        tools=(_tool("delegate"),),
    )
    request = ModelMcpToolsListRequest.model_validate(
        {"overlay": {"allowed_tools": []}}
    )
    assert HandlerMcpToolsList(_Reader([row])).handle(request).tools == ()


def test_projection_source_reads_the_dsn_from_the_env_the_contract_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_name = packaged_contract_value(PACKAGE, *DSN_ENV_KEY)
    monkeypatch.delenv(env_name, raising=False)
    reader = packaged_factory(PACKAGE, "config", "registry_factory")()
    with pytest.raises(McpServerRefusalError) as raised:
        reader.read_registrations()
    assert raised.value.refusal is EnumMcpServerRefusal.REGISTRY_UNREADABLE
    assert env_name in str(raised.value)


def test_projection_rows_pass_through_the_factory_reader() -> None:
    projection = SimpleNamespace(
        entity_id="node_x",
        current_state=SimpleNamespace(value="active"),
        capabilities=SimpleNamespace(mcp=None),
    )
    reader = packaged_factory(PACKAGE, "config", "registry_factory")(
        lambda: [projection]
    )
    assert [r.node_name for r in reader.read_registrations()] == ["node_x"]
