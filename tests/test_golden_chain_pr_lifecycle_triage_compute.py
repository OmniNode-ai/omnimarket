# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_pr_lifecycle_triage_compute.

Verifies PR triage classification logic using pure inventory data.
Zero network calls — all state is injected via ModelPrInventoryItem.

Related:
    - OMN-8083: Create pr_lifecycle_triage_compute Node
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_pr_lifecycle_triage import (
    HandlerPrLifecycleTriage,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_pr_triage_category import (
    EnumPrTriageCategory,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_pr_inventory_item import (
    ModelPrInventoryItem,
)


@pytest.mark.unit
class TestPrLifecycleTriageComputeGoldenChain:
    """Golden chain: PR inventory events in -> triage events out."""

    async def test_green_pr(self, event_bus: EventBusInmemory) -> None:
        """PR with passing CI, approved, no conflicts, no open threads -> GREEN."""
        handler = HandlerPrLifecycleTriage()
        correlation_id = uuid4()
        prs = (
            ModelPrInventoryItem(
                pr_number=101,
                repo="OmniNode-ai/omnimarket",
                title="feat: add triage node",
                branch="jonah/omn-8083",
                ci_status="passing",
                has_conflicts=False,
                approved=True,
                review_count=1,
                open_threads=0,
            ),
        )

        result = await handler.handle(correlation_id=correlation_id, prs=prs)

        assert result.correlation_id == correlation_id
        assert len(result.results) == 1
        assert result.results[0].category == EnumPrTriageCategory.GREEN
        assert result.total_green == 1
        assert result.total_red == 0
        assert result.total_conflicted == 0
        assert result.total_needs_review == 0

    async def test_red_pr_failing_ci(self, event_bus: EventBusInmemory) -> None:
        """PR with failing CI -> RED regardless of approval."""
        handler = HandlerPrLifecycleTriage()
        correlation_id = uuid4()
        prs = (
            ModelPrInventoryItem(
                pr_number=202,
                repo="OmniNode-ai/omnimarket",
                title="fix: broken handler",
                ci_status="failing",
                has_conflicts=False,
                approved=True,
            ),
        )

        result = await handler.handle(correlation_id=correlation_id, prs=prs)

        assert result.results[0].category == EnumPrTriageCategory.RED
        assert result.total_red == 1

    async def test_conflicted_pr(self, event_bus: EventBusInmemory) -> None:
        """PR with merge conflicts -> CONFLICTED regardless of CI."""
        handler = HandlerPrLifecycleTriage()
        correlation_id = uuid4()
        prs = (
            ModelPrInventoryItem(
                pr_number=303,
                repo="OmniNode-ai/omnimarket",
                title="refactor: big change",
                ci_status="passing",
                has_conflicts=True,
                approved=True,
            ),
        )

        result = await handler.handle(correlation_id=correlation_id, prs=prs)

        assert result.results[0].category == EnumPrTriageCategory.CONFLICTED
        assert result.total_conflicted == 1

    async def test_needs_review_no_approval(self, event_bus: EventBusInmemory) -> None:
        """PR with passing CI but no approval -> NEEDS_REVIEW."""
        handler = HandlerPrLifecycleTriage()
        correlation_id = uuid4()
        prs = (
            ModelPrInventoryItem(
                pr_number=404,
                repo="OmniNode-ai/omnimarket",
                title="chore: update deps",
                ci_status="passing",
                has_conflicts=False,
                approved=False,
            ),
        )

        result = await handler.handle(correlation_id=correlation_id, prs=prs)

        assert result.results[0].category == EnumPrTriageCategory.NEEDS_REVIEW
        assert result.total_needs_review == 1

    async def test_needs_review_pending_ci(self, event_bus: EventBusInmemory) -> None:
        """PR with pending CI (not yet approved) -> NEEDS_REVIEW."""
        handler = HandlerPrLifecycleTriage()
        correlation_id = uuid4()
        prs = (
            ModelPrInventoryItem(
                pr_number=505,
                repo="OmniNode-ai/omnimarket",
                ci_status="pending",
                has_conflicts=False,
                approved=False,
            ),
        )

        result = await handler.handle(correlation_id=correlation_id, prs=prs)

        assert result.results[0].category == EnumPrTriageCategory.NEEDS_REVIEW

    async def test_needs_review_open_threads(self, event_bus: EventBusInmemory) -> None:
        """PR approved and CI passing but with open threads -> NEEDS_REVIEW."""
        handler = HandlerPrLifecycleTriage()
        correlation_id = uuid4()
        prs = (
            ModelPrInventoryItem(
                pr_number=606,
                repo="OmniNode-ai/omnimarket",
                ci_status="passing",
                has_conflicts=False,
                approved=True,
                open_threads=2,
            ),
        )

        result = await handler.handle(correlation_id=correlation_id, prs=prs)

        assert result.results[0].category == EnumPrTriageCategory.NEEDS_REVIEW
        assert "2 unresolved" in result.results[0].reason

    async def test_conflicted_takes_priority_over_failing_ci(
        self, event_bus: EventBusInmemory
    ) -> None:
        """CONFLICTED takes priority over RED when both conditions are true."""
        handler = HandlerPrLifecycleTriage()
        correlation_id = uuid4()
        prs = (
            ModelPrInventoryItem(
                pr_number=707,
                repo="OmniNode-ai/omnimarket",
                ci_status="failing",
                has_conflicts=True,
                approved=False,
            ),
        )

        result = await handler.handle(correlation_id=correlation_id, prs=prs)

        assert result.results[0].category == EnumPrTriageCategory.CONFLICTED

    async def test_mixed_pr_batch(self, event_bus: EventBusInmemory) -> None:
        """Multiple PRs in a batch are classified independently."""
        handler = HandlerPrLifecycleTriage()
        correlation_id = uuid4()
        prs = (
            ModelPrInventoryItem(
                pr_number=1,
                repo="OmniNode-ai/omnimarket",
                ci_status="passing",
                has_conflicts=False,
                approved=True,
                open_threads=0,
            ),
            ModelPrInventoryItem(
                pr_number=2,
                repo="OmniNode-ai/omnimarket",
                ci_status="failing",
                has_conflicts=False,
                approved=False,
            ),
            ModelPrInventoryItem(
                pr_number=3,
                repo="OmniNode-ai/omnimarket",
                ci_status="passing",
                has_conflicts=True,
                approved=False,
            ),
            ModelPrInventoryItem(
                pr_number=4,
                repo="OmniNode-ai/omnimarket",
                ci_status="passing",
                has_conflicts=False,
                approved=False,
            ),
        )

        result = await handler.handle(correlation_id=correlation_id, prs=prs)

        assert len(result.results) == 4
        assert result.total_green == 1
        assert result.total_red == 1
        assert result.total_conflicted == 1
        assert result.total_needs_review == 1

    async def test_empty_pr_batch(self, event_bus: EventBusInmemory) -> None:
        """Empty inventory batch returns zero counts."""
        handler = HandlerPrLifecycleTriage()
        correlation_id = uuid4()

        result = await handler.handle(correlation_id=correlation_id, prs=())

        assert len(result.results) == 0
        assert result.total_green == 0
        assert result.total_red == 0
        assert result.total_conflicted == 0
        assert result.total_needs_review == 0

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler can be wired to event bus for inventory->triage event flow."""
        handler = HandlerPrLifecycleTriage()
        triage_events: list[dict[str, object]] = []

        async def on_inventory_event(message: object) -> None:
            correlation_id = uuid4()
            prs = (
                ModelPrInventoryItem(
                    pr_number=999,
                    repo="OmniNode-ai/omnimarket",
                    ci_status="passing",
                    has_conflicts=False,
                    approved=True,
                    open_threads=0,
                ),
            )
            result = await handler.handle(correlation_id=correlation_id, prs=prs)
            event = {
                "correlation_id": str(result.correlation_id),
                "total_green": result.total_green,
                "total_red": result.total_red,
                "total_conflicted": result.total_conflicted,
                "total_needs_review": result.total_needs_review,
                "count": len(result.results),
            }
            triage_events.append(event)
            await event_bus.publish(
                "onex.evt.omnimarket.pr-lifecycle-triage-completed.v1",
                key=None,
                value=json.dumps(event).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            "onex.evt.omnimarket.pr-lifecycle-inventory-completed.v1",
            on_message=on_inventory_event,
            group_id="test-triage",
        )

        await event_bus.publish(
            "onex.evt.omnimarket.pr-lifecycle-inventory-completed.v1",
            key=None,
            value=b'{"inventory": "completed"}',
        )

        assert len(triage_events) == 1
        assert triage_events[0]["total_green"] == 1
        assert triage_events[0]["total_red"] == 0

        history = await event_bus.get_event_history(
            topic="onex.evt.omnimarket.pr-lifecycle-triage-completed.v1"
        )
        assert len(history) == 1

        await event_bus.close()
