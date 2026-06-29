# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPrLifecycleFix routes the receipt_evidence_source_autobind reason.

Verifies the new OMN-13317 F1 block reason dispatches to the OCC autobind
adapter via a protocol-injected mock — zero infrastructure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
    HandlerPrLifecycleFix,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)


class _RecordingAutobindAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str | None]] = []

    async def autobind_evidence_source(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> str:
        self.calls.append((repo, pr_number, ticket_id))
        return f"autobound OCC for {repo}#{pr_number}"


def _command(ticket_id: str | None) -> ModelPrLifecycleFixCommand:
    return ModelPrLifecycleFixCommand(
        correlation_id=uuid4(),
        pr_number=2043,
        repo="OmniNode-ai/omnibase_infra",
        block_reason=EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND,
        ticket_id=ticket_id,
        requested_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
class TestHandlerRoutesAutobind:
    async def test_routes_to_autobind_adapter_with_ticket(self) -> None:
        adapter = _RecordingAutobindAdapter()
        handler = HandlerPrLifecycleFix(occ_autobind_adapter=adapter)

        result = await handler.handle(_command("OMN-9999"))

        assert result.fix_applied is True
        assert result.error is None
        assert result.block_reason is EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND
        assert adapter.calls == [("OmniNode-ai/omnibase_infra", 2043, "OMN-9999")]

    async def test_routes_to_autobind_adapter_without_ticket(self) -> None:
        """ticket_id is optional — the adapter detects it from the PR itself."""
        adapter = _RecordingAutobindAdapter()
        handler = HandlerPrLifecycleFix(occ_autobind_adapter=adapter)

        result = await handler.handle(_command(None))

        assert result.fix_applied is True
        assert adapter.calls == [("OmniNode-ai/omnibase_infra", 2043, None)]

    async def test_default_noop_adapter_describes_action(self) -> None:
        handler = HandlerPrLifecycleFix()
        result = await handler.handle(_command("OMN-9999"))

        assert result.fix_applied is True
        assert "[noop] would autobind" in result.fix_action
