# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection-backed registry reader: tools/list answers through the node
when the installed infra carries the typed mcp tools list, and refuses by name
when it does not (OMN-15920)."""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from uuid import UUID

import pytest

from omnimarket.nodes.node_mcp_server_effect.handlers.handler_mcp_registry_projection_rows import (
    HandlerMcpRegistryProjectionRows,
)
from omnimarket.nodes.node_mcp_server_effect.handlers.handler_mcp_tools_list import (
    HandlerMcpToolsList,
)
from omnimarket.nodes.node_mcp_server_effect.models.model_mcp_tools_list import (
    ModelMcpOverlay,
    ModelMcpRegistryReadRequest,
    ModelMcpToolsListRequest,
    ModelMcpToolsListResult,
)
from omnimarket.nodes.node_mcp_server_effect.models.refusal import (
    EnumMcpServerRefusal,
    McpServerRefusalError,
)

ENTITY = UUID("00000000-0000-4000-8000-000000000001")
TOOL = {
    "tool_name": "alpha",
    "description": "alpha tool",
    "timeout_seconds": 30,
    "input_model": "pkg.models.AlphaInput",
    "output_model": "pkg.models.AlphaOutput",
    "command_topic": "onex.cmd.test.alpha.v1",
    "completed_topic": "onex.evt.test.alpha-completed.v1",
    "failed_topic": "onex.evt.test.alpha-failed.v1",
}


def _call(projections: Sequence[object]) -> ModelMcpToolsListResult:
    reader = HandlerMcpRegistryProjectionRows(lambda: projections)
    return HandlerMcpToolsList(reader).handle(
        ModelMcpToolsListRequest(overlay=ModelMcpOverlay(allowed_tools={"alpha"}))
    )


def _fake(mcp: object, state: str = "active") -> SimpleNamespace:
    return SimpleNamespace(
        entity_id=ENTITY,
        current_state=SimpleNamespace(value=state),
        capabilities=SimpleNamespace(mcp=mcp),
    )


@pytest.mark.unit
def test_projection_with_a_tools_list_answers_through_the_node() -> None:
    mcp = SimpleNamespace(expose=True, tools=[SimpleNamespace(**TOOL)])
    result = _call([_fake(mcp)])
    assert [t.tool_name for t in result.tools] == ["alpha"]
    assert result.tools[0].node_name == str(ENTITY)
    assert result.tools[0].command_topic == "onex.cmd.test.alpha.v1"


@pytest.mark.unit
def test_inactive_projection_is_not_listed() -> None:
    mcp = SimpleNamespace(expose=True, tools=[SimpleNamespace(**TOOL)])
    assert _call([_fake(mcp, state="liveness_expired")]).tools == ()


@pytest.mark.unit
def test_projection_without_mcp_block_is_not_listed() -> None:
    assert _call([_fake(None)]).tools == ()


@pytest.mark.unit
def test_old_single_tool_block_is_refused_by_name() -> None:
    old_block = SimpleNamespace(expose=True, tool_name="alpha")
    with pytest.raises(McpServerRefusalError) as exc:
        _call([_fake(old_block)])
    assert exc.value.refusal is EnumMcpServerRefusal.INFRA_TOOLS_LIST_ABSENT


@pytest.mark.unit
def test_real_infra_projection_lists_or_refuses_by_name() -> None:
    """The same real infra projection: listed when the installed infra reads
    the typed tools list, refused by name when it drops it."""
    from omnibase_core.enums import EnumNodeKind
    from omnibase_infra.enums import EnumRegistrationState
    from omnibase_infra.models.projection import ModelRegistrationProjection
    from omnibase_infra.models.registration.model_node_capabilities import (
        ModelNodeCapabilities,
    )

    caps = ModelNodeCapabilities.model_validate(
        {"mcp": {"expose": True, "tools": [TOOL]}}
    )
    projection = ModelRegistrationProjection(
        entity_id=ENTITY,
        current_state=EnumRegistrationState.ACTIVE,
        node_type=EnumNodeKind.ORCHESTRATOR,
        node_version="1.0.0",
        capabilities=caps,
        last_applied_event_id=ENTITY,
        last_applied_offset=0,
        registered_at="2026-11-26T00:00:00+00:00",
        updated_at="2026-11-26T00:00:00+00:00",
    )
    if hasattr(caps.mcp, "tools"):
        assert [t.tool_name for t in _call([projection]).tools] == ["alpha"]
    else:
        with pytest.raises(McpServerRefusalError) as exc:
            _call([projection])
        assert exc.value.refusal is EnumMcpServerRefusal.INFRA_TOOLS_LIST_ABSENT


@pytest.mark.unit
def test_handle_returns_the_registry_rows_the_reader_reads() -> None:
    mcp = SimpleNamespace(expose=True, tools=[SimpleNamespace(**TOOL)])
    reader = HandlerMcpRegistryProjectionRows(lambda: [_fake(mcp)])
    rows = reader.handle(ModelMcpRegistryReadRequest()).rows
    assert [r.node_name for r in rows] == [str(ENTITY)]
    assert [t.tool_name for t in rows[0].tools] == ["alpha"]
