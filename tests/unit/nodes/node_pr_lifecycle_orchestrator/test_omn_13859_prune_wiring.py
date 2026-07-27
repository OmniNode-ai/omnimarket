# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13859: prove pr_lifecycle_orchestrator drives the worktree-prune effect.

Focused unit tests on ``_prune_merged_worktrees`` — the POST_MERGE_TAIL seam —
with a spy prune handler injected. Verifies:
  * one prune command per (ticket, repo) for each merged PR;
  * branch is recovered from inventory for provenance;
  * PruneResult aggregates pruned / flagged-dirty / skipped outcomes;
  * the prune step is inert (no injected handler required) via the stub.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)

from omnimarket.events.worktree_prune import (
    EnumPruneOutcome,
    ModelWorktreePruneResult,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    HandlerPrLifecycleOrchestrator,
    _StubPruneHandler,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
    EnumPrCategory,
    InventoryResult,
    PrRecord,
    PruneResult,
    TriageRecord,
)


class SpyPruneHandler:
    """Records prune commands and returns scripted outcomes by ticket."""

    def __init__(self, outcomes: dict[str, EnumPruneOutcome] | None = None) -> None:
        self._outcomes = outcomes or {}
        self.commands: list[Any] = []

    async def handle(self, command: Any) -> ModelWorktreePruneResult:
        self.commands.append(command)
        from datetime import UTC, datetime

        return ModelWorktreePruneResult(
            correlation_id=command.correlation_id,
            ticket_id=command.ticket_id,
            repo=command.repo,
            outcome=self._outcomes.get(command.ticket_id, EnumPruneOutcome.PRUNED),
            completed_at=datetime.now(tz=UTC),
        )


def _orchestrator(prune: Any) -> HandlerPrLifecycleOrchestrator:
    bus = MagicMock(spec=ProtocolEventBusPublisher)
    return HandlerPrLifecycleOrchestrator(prune=prune, event_bus=bus)


@pytest.mark.unit
async def test_prune_one_command_per_ticket_with_branch_from_inventory() -> None:
    spy = SpyPruneHandler()
    orch = _orchestrator(spy)
    merged = (
        TriageRecord(
            pr_number=42,
            repo="OmniNode-ai/omnimarket",
            category=EnumPrCategory.GREEN,
            ticket_ids=("OMN-13859",),
        ),
    )
    inventory = InventoryResult(
        prs=(
            PrRecord(
                pr_number=42,
                repo="OmniNode-ai/omnimarket",
                branch="jonah/omn-13859-x",
                ticket_ids=("OMN-13859",),
            ),
        ),
        total_collected=1,
    )

    result = await orch._prune_merged_worktrees(
        merged=merged, inventory=inventory, correlation_id=uuid4()
    )

    assert len(spy.commands) == 1
    cmd = spy.commands[0]
    assert cmd.ticket_id == "OMN-13859"
    assert cmd.repo == "OmniNode-ai/omnimarket"
    assert cmd.branch == "jonah/omn-13859-x"
    assert cmd.pr_number == 42
    assert result == PruneResult(
        worktrees_pruned=1, worktrees_flagged_dirty=0, worktrees_skipped=0
    )


@pytest.mark.unit
async def test_prune_aggregates_dirty_and_skipped() -> None:
    spy = SpyPruneHandler(
        outcomes={
            "OMN-1": EnumPruneOutcome.PRUNED,
            "OMN-2": EnumPruneOutcome.SKIPPED_DIRTY,
            "OMN-3": EnumPruneOutcome.SKIPPED_NOT_FOUND,
        }
    )
    orch = _orchestrator(spy)
    merged = tuple(
        TriageRecord(
            pr_number=i,
            repo="OmniNode-ai/omnimarket",
            category=EnumPrCategory.GREEN,
            ticket_ids=(f"OMN-{i}",),
        )
        for i in (1, 2, 3)
    )

    result = await orch._prune_merged_worktrees(
        merged=merged, inventory=None, correlation_id=uuid4()
    )

    assert result == PruneResult(
        worktrees_pruned=1, worktrees_flagged_dirty=1, worktrees_skipped=1
    )


@pytest.mark.unit
async def test_prune_handler_exception_is_swallowed() -> None:
    class Boom:
        async def handle(self, command: Any) -> Any:
            raise RuntimeError("git blew up")

    orch = _orchestrator(Boom())
    merged = (
        TriageRecord(
            pr_number=1,
            repo="omnimarket",
            category=EnumPrCategory.GREEN,
            ticket_ids=("OMN-1",),
        ),
    )

    # Must not raise — worktree GC is best-effort.
    result = await orch._prune_merged_worktrees(
        merged=merged, inventory=None, correlation_id=uuid4()
    )
    assert result.worktrees_skipped == 1


@pytest.mark.unit
async def test_stub_prune_handler_is_inert() -> None:
    result = await _StubPruneHandler().handle(object())
    assert result is None


# ---------------------------------------------------------------------------
# OMN-15251 — the merge-target seam.
#
# The superseded classifier is inert unless the orchestrator actually supplies
# merge_target_ref. These drive the real orchestrator seam (not a second
# independent unit suite) so a field-name or prefix drift fails here.
# ---------------------------------------------------------------------------


def _merged_one(
    ticket: str = "OMN-15251", pr_number: int = 77
) -> tuple[TriageRecord, ...]:
    return (
        TriageRecord(
            pr_number=pr_number,
            repo="OmniNode-ai/omnimarket",
            category=EnumPrCategory.GREEN,
            ticket_ids=(ticket,),
        ),
    )


@pytest.mark.unit
async def test_merge_target_ref_is_derived_from_inventory_base_ref() -> None:
    """base_ref='dev' must reach the prune command as 'origin/dev'."""
    spy = SpyPruneHandler()
    orch = _orchestrator(spy)
    inventory = InventoryResult(
        prs=(
            PrRecord(
                pr_number=77,
                repo="OmniNode-ai/omnimarket",
                branch="jonah/omn-15251-x",
                base_ref="dev",
                ticket_ids=("OMN-15251",),
            ),
        ),
        total_collected=1,
    )

    await orch._prune_merged_worktrees(
        merged=_merged_one(), inventory=inventory, correlation_id=uuid4()
    )

    assert spy.commands[0].merge_target_ref == "origin/dev"


@pytest.mark.unit
async def test_missing_base_ref_sends_no_merge_target_and_fails_closed() -> None:
    """Unknown base branch must not be guessed — the effect then preserves."""
    spy = SpyPruneHandler()
    orch = _orchestrator(spy)
    inventory = InventoryResult(
        prs=(
            PrRecord(
                pr_number=77,
                repo="OmniNode-ai/omnimarket",
                branch="jonah/omn-15251-x",
                ticket_ids=("OMN-15251",),
            ),
        ),
        total_collected=1,
    )

    await orch._prune_merged_worktrees(
        merged=_merged_one(), inventory=inventory, correlation_id=uuid4()
    )

    assert spy.commands[0].merge_target_ref is None


@pytest.mark.unit
async def test_superseded_prune_counts_as_pruned_not_dirty() -> None:
    """PRUNED_SUPERSEDED must aggregate as pruned, never as a dirty decision."""
    spy = SpyPruneHandler(outcomes={"OMN-15251": EnumPruneOutcome.PRUNED_SUPERSEDED})
    orch = _orchestrator(spy)

    result = await orch._prune_merged_worktrees(
        merged=_merged_one(), inventory=None, correlation_id=uuid4()
    )

    assert result == PruneResult(
        worktrees_pruned=1, worktrees_flagged_dirty=0, worktrees_skipped=0
    )
