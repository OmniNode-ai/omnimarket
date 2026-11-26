# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""List the tools: ACTIVE registration, mcp.expose true, overlay allows."""

from __future__ import annotations

from omnimarket.nodes.node_mcp_server_effect.models.model_mcp_tools_list import (
    EnumMcpRegistrationState,
    ModelMcpToolListing,
    ModelMcpToolsListRequest,
    ModelMcpToolsListResult,
)
from omnimarket.nodes.node_mcp_server_effect.models.refusal import (
    EnumMcpServerRefusal,
    McpServerRefusalError,
)
from omnimarket.nodes.node_mcp_server_effect.ports.protocol_mcp_registry_reader import (
    ProtocolMcpRegistryReader,
)


class HandlerMcpToolsList:
    """One eligibility rule; the code names no tool."""

    def __init__(self, registry: ProtocolMcpRegistryReader) -> None:
        self._registry = registry

    def handle(self, request: ModelMcpToolsListRequest) -> ModelMcpToolsListResult:
        try:
            rows = tuple(self._registry.read_registrations())
        except McpServerRefusalError:
            raise
        except Exception as exc:
            raise McpServerRefusalError(
                EnumMcpServerRefusal.REGISTRY_UNREADABLE,
                f"the registry could not be read: {exc}",
            ) from exc
        allowed = request.overlay.allowed_tools
        listed: dict[str, ModelMcpToolListing] = {}
        for row in rows:
            if row.state is not EnumMcpRegistrationState.ACTIVE:
                continue
            if not row.expose:
                continue
            for tool in row.tools:
                if tool.tool_name not in allowed:
                    continue
                if tool.tool_name in listed:
                    raise McpServerRefusalError(
                        EnumMcpServerRefusal.DUPLICATE_TOOL_NAME,
                        f"tool {tool.tool_name!r} is declared by both "
                        f"{listed[tool.tool_name].node_name!r} and {row.node_name!r}",
                    )
                listed[tool.tool_name] = ModelMcpToolListing(
                    tool_name=tool.tool_name,
                    node_name=row.node_name,
                    description=tool.description,
                    timeout_seconds=tool.timeout_seconds,
                    input_model=tool.input_model,
                    output_model=tool.output_model,
                    command_topic=tool.command_topic,
                    completed_topic=tool.completed_topic,
                    failed_topic=tool.failed_topic,
                )
        return ModelMcpToolsListResult(tools=tuple(listed.values()))
