# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory fakes of the terminal writer's ports; they mirror the Postgres store's rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_gateway_terminal_applied import (
    EnumGatewayTerminalStoreOutcome,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_terminal_update import (
    ModelTerminalUpdate,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.protocols.protocol_gateway_terminal_store import (
    GatewayTerminalPublishError,
    GatewayTerminalStoreError,
)


@dataclass
class FakeRow:
    tenant_id: UUID
    correlation_id: UUID
    status: str = "published"
    workflow_id: UUID = field(default_factory=uuid4)
    columns: dict[str, Any] = field(default_factory=dict)


class FakeTerminalStore:
    """Rows of every status; lookup, ambiguity and the status guard as the real store has them."""

    def __init__(
        self, rows: list[FakeRow] | None = None, *, fail: bool = False
    ) -> None:
        self.rows = rows or []
        self.fail = fail
        self.calls = 0

    async def apply_terminal(
        self, update: ModelTerminalUpdate
    ) -> tuple[EnumGatewayTerminalStoreOutcome, UUID | None]:
        self.calls += 1
        if self.fail:
            raise GatewayTerminalStoreError("OSError")
        matches = [
            r
            for r in self.rows
            if r.tenant_id == update.tenant_id
            and r.correlation_id == update.correlation_id
        ]
        if not matches:
            return EnumGatewayTerminalStoreOutcome.NO_ROW, None
        if len(matches) > 1:
            return EnumGatewayTerminalStoreOutcome.AMBIGUOUS, None
        row = matches[0]
        if row.status in ("completed", "failed"):
            return EnumGatewayTerminalStoreOutcome.ALREADY_TERMINAL, row.workflow_id
        row.status = update.status
        row.columns = update.model_dump(
            exclude={"tenant_id", "correlation_id", "status"}
        )
        return EnumGatewayTerminalStoreOutcome.APPLIED, row.workflow_id


class FakeDeadLetter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    async def publish(self, *, topic: str, key: str, record: dict[str, Any]) -> None:
        if self.fail:
            raise GatewayTerminalPublishError("KafkaError")
        self.records.append((topic, key, record))


__all__: list[str] = ["FakeDeadLetter", "FakeRow", "FakeTerminalStore"]
