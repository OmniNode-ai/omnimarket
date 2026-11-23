# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the terminal writer: the row store and the dead-letter publisher."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_gateway_terminal_applied import (
    EnumGatewayTerminalStoreOutcome,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_terminal_update import (
    ModelTerminalUpdate,
)


class GatewayTerminalStoreError(RuntimeError):
    """The store could not be reached or refused the statement; never acknowledged."""


class GatewayTerminalPublishError(RuntimeError):
    """The dead-letter publish failed; never acknowledged."""


@runtime_checkable
class ProtocolGatewayTerminalStore(Protocol):
    """Apply one terminal to the one gateway row of a tenant and correlation, in one transaction."""

    async def apply_terminal(
        self, update: ModelTerminalUpdate
    ) -> tuple[EnumGatewayTerminalStoreOutcome, UUID | None]:
        """Return the outcome and the workflow id of the row, when there is exactly one."""


@runtime_checkable
class ProtocolGatewayTerminalDeadLetter(Protocol):
    """Publish one dead-letter record."""

    async def publish(self, *, topic: str, key: str, record: dict[str, Any]) -> None:
        """Raises GatewayTerminalPublishError on every failure path."""


__all__: list[str] = [
    "GatewayTerminalPublishError",
    "GatewayTerminalStoreError",
    "ProtocolGatewayTerminalDeadLetter",
    "ProtocolGatewayTerminalStore",
]
