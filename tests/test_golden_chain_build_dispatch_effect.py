"""Golden chain tests for node_build_dispatch_effect.

Verifies delegation payload construction and dry-run behavior.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.models.enum_buildability import EnumBuildability
from omnimarket.nodes.node_build_dispatch_effect.handlers.handler_build_dispatch import (
    HandlerBuildDispatch,
)
from omnimarket.nodes.node_build_dispatch_effect.models.model_build_dispatch_result import (
    ModelBuildDispatchResult,
)
from omnimarket.nodes.node_build_dispatch_effect.models.model_build_target import (
    ModelBuildTarget,
)


def _target(ticket_id: str) -> ModelBuildTarget:
    return ModelBuildTarget(
        ticket_id=ticket_id,
        title=f"Build {ticket_id}",
        buildability=EnumBuildability.AUTO_BUILDABLE,
    )


@pytest.mark.unit
class TestBuildDispatchEffectGoldenChain:
    """Golden chain: build targets -> dispatch outcomes + delegation payloads."""

    async def test_dry_run_dispatches_all(self) -> None:
        """Dry run marks all targets as dispatched without payloads."""
        handler = HandlerBuildDispatch()
        targets = (_target("OMN-1"), _target("OMN-2"))

        result: ModelBuildDispatchResult = await handler.handle(
            correlation_id=uuid4(),
            targets=targets,
            dry_run=True,
        )

        assert result.total_dispatched == 2
        assert result.total_failed == 0
        assert all(o.dispatched for o in result.outcomes)
        assert len(result.delegation_payloads) == 0

    async def test_real_dispatch_creates_payloads(self) -> None:
        """Non-dry-run creates delegation payloads for each target."""
        handler = HandlerBuildDispatch()
        targets = (_target("OMN-1"), _target("OMN-2"))

        result = await handler.handle(
            correlation_id=uuid4(),
            targets=targets,
            dry_run=False,
        )

        assert result.total_dispatched == 2
        assert result.total_failed == 0
        assert len(result.delegation_payloads) == 2
        assert all(p.topic for p in result.delegation_payloads)
        assert all(p.correlation_id for p in result.delegation_payloads)

    async def test_empty_targets(self) -> None:
        """Empty target list returns empty result."""
        handler = HandlerBuildDispatch()
        result = await handler.handle(
            correlation_id=uuid4(),
            targets=(),
            dry_run=False,
        )

        assert result.total_dispatched == 0
        assert result.total_failed == 0
        assert len(result.outcomes) == 0

    async def test_duplicate_ticket_ids_raises(self) -> None:
        """Duplicate ticket_ids in batch raises ValueError."""
        handler = HandlerBuildDispatch()
        targets = (_target("OMN-1"), _target("OMN-1"))

        with pytest.raises(ValueError, match="Duplicate ticket_id"):
            await handler.handle(
                correlation_id=uuid4(),
                targets=targets,
                dry_run=False,
            )
