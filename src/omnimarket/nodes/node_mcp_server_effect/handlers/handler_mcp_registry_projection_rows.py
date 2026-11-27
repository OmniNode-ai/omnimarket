# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registry reader over the registration projections the infra registry serves."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from omnimarket.nodes.node_mcp_server_effect.models.model_mcp_tools_list import (
    EnumMcpRegistrationState,
    ModelMcpRegistryReadRequest,
    ModelMcpRegistryRow,
    ModelMcpRegistryRows,
    ModelMcpToolEntry,
)
from omnimarket.nodes.node_mcp_server_effect.models.refusal import (
    EnumMcpServerRefusal,
    McpServerRefusalError,
)


class HandlerMcpRegistryProjectionRows:
    """Maps each projection's ``capabilities.mcp`` block to a registry row.

    The projections come from the infra registry reader. An infra that does not
    carry the typed ``mcp.tools`` list on the block is a named refusal, never an
    empty list.
    """

    def __init__(self, projections: Callable[[], Sequence[Any]] | None = None) -> None:
        self._projections = projections

    def handle(self, request: ModelMcpRegistryReadRequest) -> ModelMcpRegistryRows:
        return ModelMcpRegistryRows(rows=tuple(self.read_registrations()))

    def read_registrations(self) -> Sequence[ModelMcpRegistryRow]:
        if self._projections is None:
            raise McpServerRefusalError(
                EnumMcpServerRefusal.REGISTRY_UNREADABLE,
                "no projections source was given to the registry reader",
            )
        return tuple(self._row(proj) for proj in self._projections())

    @staticmethod
    def _row(proj: Any) -> ModelMcpRegistryRow:
        node_name = str(proj.entity_id)
        state = EnumMcpRegistrationState(proj.current_state.value)
        mcp = proj.capabilities.mcp
        if mcp is None:
            return ModelMcpRegistryRow(node_name=node_name, state=state)
        if not hasattr(mcp, "tools"):
            raise McpServerRefusalError(
                EnumMcpServerRefusal.INFRA_TOOLS_LIST_ABSENT,
                f"the mcp block of {node_name!r} has no tools list; the installed "
                "infra predates the typed tools list",
            )
        tools = tuple(
            ModelMcpToolEntry(
                tool_name=t.tool_name,
                description=t.description,
                timeout_seconds=t.timeout_seconds,
                input_model=t.input_model,
                output_model=t.output_model,
                command_topic=t.command_topic,
                completed_topic=t.completed_topic,
                failed_topic=t.failed_topic,
            )
            for t in mcp.tools
        )
        return ModelMcpRegistryRow(
            node_name=node_name, state=state, expose=bool(mcp.expose), tools=tools
        )
