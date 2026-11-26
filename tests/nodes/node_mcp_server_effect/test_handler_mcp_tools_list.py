# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""tools/list of node_mcp_server_effect: ACTIVE + mcp.expose + overlay allow."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from omnimarket.nodes.node_mcp_server_effect.handlers.handler_mcp_tools_list import (
    HandlerMcpToolsList,
)
from omnimarket.nodes.node_mcp_server_effect.models.model_mcp_tools_list import (
    EnumMcpRegistrationState,
    ModelMcpOverlay,
    ModelMcpRegistryRow,
    ModelMcpToolEntry,
    ModelMcpToolsListRequest,
)
from omnimarket.nodes.node_mcp_server_effect.models.refusal import (
    EnumMcpServerRefusal,
    McpServerRefusalError,
)


class _Mcp:
    def __init__(self, expose: bool, tools: tuple[ModelMcpToolEntry, ...]) -> None:
        self.expose = expose
        self.tools = tools


def _mcp(*names: str, expose: bool = True) -> _Mcp:
    return _Mcp(
        expose,
        tuple(
            ModelMcpToolEntry(
                tool_name=n,
                description=f"{n} tool",
                timeout_seconds=30,
                input_model=f"pkg.models.{n.title()}Input",
                output_model=f"pkg.models.{n.title()}Output",
                command_topic=f"onex.cmd.test.{n}.v1",
                completed_topic=f"onex.evt.test.{n}-completed.v1",
                failed_topic=f"onex.evt.test.{n}-failed.v1",
            )
            for n in names
        ),
    )


class _Reader:
    def __init__(self, rows: Sequence[ModelMcpRegistryRow] | Exception) -> None:
        self._rows = rows

    def read_registrations(self) -> Sequence[ModelMcpRegistryRow]:
        if isinstance(self._rows, Exception):
            raise self._rows
        return self._rows


def _row(
    node: str,
    state: EnumMcpRegistrationState,
    mcp: _Mcp | None,
) -> ModelMcpRegistryRow:
    if mcp is None:
        return ModelMcpRegistryRow(node_name=node, state=state)
    return ModelMcpRegistryRow(
        node_name=node, state=state, expose=mcp.expose, tools=mcp.tools
    )


def _list(rows: Sequence[ModelMcpRegistryRow], allowed: set[str]) -> list[str]:
    result = HandlerMcpToolsList(_Reader(rows)).handle(
        ModelMcpToolsListRequest(overlay=ModelMcpOverlay(allowed_tools=allowed))
    )
    return [t.tool_name for t in result.tools]


A = EnumMcpRegistrationState.ACTIVE


@pytest.mark.unit
def test_active_exposed_allowed_node_is_listed() -> None:
    assert _list([_row("n1", A, _mcp("alpha"))], {"alpha"}) == ["alpha"]


@pytest.mark.unit
def test_inactive_node_is_not_listed() -> None:
    rows = [
        _row("n1", EnumMcpRegistrationState.LIVENESS_EXPIRED, _mcp("alpha")),
        _row("n2", EnumMcpRegistrationState.PENDING_REGISTRATION, _mcp("beta")),
    ]
    assert _list(rows, {"alpha", "beta"}) == []


@pytest.mark.unit
def test_unexposed_node_is_not_listed() -> None:
    rows = [
        _row("n1", A, _mcp("alpha", expose=False)),
        _row("n2", A, None),
    ]
    assert _list(rows, {"alpha"}) == []


@pytest.mark.unit
def test_overlay_is_default_deny() -> None:
    rows = [_row("n1", A, _mcp("alpha", "beta"))]
    assert _list(rows, set()) == []
    assert _list(rows, {"beta"}) == ["beta"]


@pytest.mark.unit
def test_listing_carries_the_contract_entry() -> None:
    result = HandlerMcpToolsList(_Reader([_row("n1", A, _mcp("alpha"))])).handle(
        ModelMcpToolsListRequest(overlay=ModelMcpOverlay(allowed_tools={"alpha"}))
    )
    tool = result.tools[0]
    assert tool.node_name == "n1"
    assert tool.description == "alpha tool"
    assert tool.command_topic == "onex.cmd.test.alpha.v1"
    assert tool.input_model == "pkg.models.AlphaInput"


@pytest.mark.unit
def test_duplicate_tool_name_is_refused_by_name() -> None:
    rows = [_row("n1", A, _mcp("alpha")), _row("n2", A, _mcp("alpha"))]
    with pytest.raises(McpServerRefusalError) as exc:
        _list(rows, {"alpha"})
    assert exc.value.refusal is EnumMcpServerRefusal.DUPLICATE_TOOL_NAME
    assert "alpha" in str(exc.value)


@pytest.mark.unit
def test_unreadable_registry_is_a_named_refusal_not_an_empty_list() -> None:
    with pytest.raises(McpServerRefusalError) as exc:
        _list_with(_Reader(RuntimeError("db down")))
    assert exc.value.refusal is EnumMcpServerRefusal.REGISTRY_UNREADABLE


def _list_with(reader: _Reader) -> object:
    return HandlerMcpToolsList(reader).handle(
        ModelMcpToolsListRequest(overlay=ModelMcpOverlay(allowed_tools={"alpha"}))
    )
