# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler-level proof of explicit phase recording in the live FSM (OMN-12570).

These tests drive the real ``HandlerPrLifecycleOrchestrator._run_sweep`` FSM
with mocked sub-handlers and assert against the durable ledger the handler
exposes via ``handler.ledger(run_id)``. They prove the orchestrator records
phase transitions *as it runs* and stamps each ledger entry with the phase it
was produced in — not after the fact from logs.

Related:
    - OMN-12570: Phase-separate branch / merge-group / post-merge checks.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    HandlerPrLifecycleOrchestrator,
    ModelPrLifecycleStartCommand,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
    EnumPrCategory,
    EnumReducerIntent,
    FixResult,
    InventoryResult,
    MergeResult,
    PrRecord,
    PrTriageResult,
    ReducerIntent,
    ReducerResult,
    TriageRecord,
)
from omnimarket.nodes.pr_ledger_native import (
    EnumPrLedgerConclusion,
    EnumPrLifecyclePhase,
)


class _RecordingBus:
    """Minimal ProtocolEventBusPublisher stand-in that records publishes."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(self, *, topic: str, key: Any, value: bytes) -> None:
        self.published.append({"topic": topic, "value": value})


class _MockInventory:
    def __init__(self, prs: tuple[PrRecord, ...]) -> None:
        self._prs = prs

    def handle(self, input_model: Any) -> InventoryResult:
        return InventoryResult(prs=self._prs, total_collected=len(self._prs))


class _MockTriage:
    def __init__(self, classified: tuple[TriageRecord, ...]) -> None:
        self._classified = classified

    async def handle(self, correlation_id: Any, prs: Any) -> PrTriageResult:
        green = sum(1 for r in self._classified if r.category == EnumPrCategory.GREEN)
        return PrTriageResult(
            classified=self._classified,
            green_count=green,
            non_green_count=len(self._classified) - green,
        )


class _MockReducer:
    def __init__(self, intents: tuple[ReducerIntent, ...]) -> None:
        self._intents = intents

    async def handle(self, *args: Any, **kwargs: Any) -> ReducerResult:
        return ReducerResult(
            intents=self._intents,
            merge_count=sum(
                1 for i in self._intents if i.intent == EnumReducerIntent.MERGE
            ),
            fix_count=sum(
                1 for i in self._intents if i.intent == EnumReducerIntent.FIX
            ),
            skip_count=sum(
                1 for i in self._intents if i.intent == EnumReducerIntent.SKIP
            ),
        )


class _MockMerge:
    async def handle(self, command: Any) -> MergeResult:
        return MergeResult(prs_merged=1, prs_failed=0)


class _MockFix:
    async def handle(self, command: Any) -> FixResult:
        return FixResult(prs_dispatched=1, prs_skipped=0)


class _Orchestrator(HandlerPrLifecycleOrchestrator):
    """Test orchestrator that bypasses real gh enumeration."""

    def __init__(self, *, _prs: tuple[PrRecord, ...], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._prs = _prs

    def _enumerate_repos(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(pr.repo for pr in self._prs))

    def _enumerate_open_pr_numbers(self, repo: str) -> tuple[int, ...]:
        return tuple(pr.pr_number for pr in self._prs if pr.repo == repo)

    def _make_merge_queue_adapter(self) -> Any:  # pragma: no cover - unused here
        raise AssertionError("merge queue adapter not expected in these tests")

    # Avoid filesystem writes in unit tests.
    def _write_result_file(self, run_id: str, result: Any) -> None:
        return None

    def _write_occ_dependency_edges_file(self, run_id: str, edges: Any) -> None:
        return None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_merge_path_records_merge_group_then_post_merge_tail() -> None:
    """A merged PR's conclusion is attributed to POST_MERGE_TAIL, not MERGING.

    The FSM must also have recorded the BRANCH_CHECKS -> MERGE_GROUP ->
    POST_MERGE_TAIL transitions explicitly.
    """
    pr = PrRecord(
        pr_number=301,
        repo="OmniNode-ai/omnimarket",
        checks_status="success",
        head_sha="sha301",
    )
    triage = TriageRecord(
        pr_number=301,
        repo="OmniNode-ai/omnimarket",
        category=EnumPrCategory.GREEN,
    )
    orch = _Orchestrator(
        _prs=(pr,),
        inventory=_MockInventory((pr,)),
        triage=_MockTriage((triage,)),
        reducer=_MockReducer(
            (
                ReducerIntent(
                    pr_number=301,
                    repo="OmniNode-ai/omnimarket",
                    intent=EnumReducerIntent.MERGE,
                ),
            )
        ),
        merge=_MockMerge(),
        fix=_MockFix(),
        event_bus=_RecordingBus(),
    )
    run_id = "20260601-190000-merge1"
    await orch.handle(
        ModelPrLifecycleStartCommand(
            correlation_id=uuid4(),
            run_id=run_id,
            merge_only=True,
        )
    )

    ledger = orch.ledger(run_id)
    entry = next(e for e in ledger.entries if e.pr_number == 301)
    assert entry.conclusion is EnumPrLedgerConclusion.MERGED
    # The terminal MERGED conclusion was recorded in POST_MERGE_TAIL.
    assert entry.last_phase is EnumPrLifecyclePhase.POST_MERGE_TAIL

    # Explicit recorded transitions cover the merge pipeline. A green PR
    # enqueues straight from triage into the merge group, then tails.
    recorded = [(t.from_phase, t.to_phase) for t in ledger.phase_transitions]
    assert (
        EnumPrLifecyclePhase.TRIAGE,
        EnumPrLifecyclePhase.MERGE_GROUP,
    ) in recorded
    assert (
        EnumPrLifecyclePhase.MERGE_GROUP,
        EnumPrLifecyclePhase.POST_MERGE_TAIL,
    ) in recorded


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fix_path_records_branch_checks_failure() -> None:
    """A non-green PR routed to FIX fails in BRANCH_CHECKS, not POST_MERGE_TAIL."""
    pr = PrRecord(
        pr_number=302,
        repo="OmniNode-ai/omnimarket",
        checks_status="failure",
        head_sha="sha302",
    )
    triage = TriageRecord(
        pr_number=302,
        repo="OmniNode-ai/omnimarket",
        category=EnumPrCategory.RED,
    )
    orch = _Orchestrator(
        _prs=(pr,),
        inventory=_MockInventory((pr,)),
        triage=_MockTriage((triage,)),
        reducer=_MockReducer(
            (
                ReducerIntent(
                    pr_number=302,
                    repo="OmniNode-ai/omnimarket",
                    intent=EnumReducerIntent.FIX,
                ),
            )
        ),
        merge=_MockMerge(),
        fix=_MockFix(),
        event_bus=_RecordingBus(),
    )
    run_id = "20260601-190000-fix1"
    await orch.handle(
        ModelPrLifecycleStartCommand(
            correlation_id=uuid4(),
            run_id=run_id,
            fix_only=True,
        )
    )

    ledger = orch.ledger(run_id)
    entry = next(e for e in ledger.entries if e.pr_number == 302)
    assert entry.conclusion is EnumPrLedgerConclusion.FAILED
    assert entry.failed_in_phase() is EnumPrLifecyclePhase.BRANCH_CHECKS

    # The transition into the branch-check phase was recorded explicitly.
    recorded = [(t.from_phase, t.to_phase) for t in ledger.phase_transitions]
    assert (
        EnumPrLifecyclePhase.TRIAGE,
        EnumPrLifecyclePhase.BRANCH_CHECKS,
    ) in recorded
    # No merge-group/post-merge transition was recorded on the fix-only path.
    assert (
        EnumPrLifecyclePhase.MERGE_GROUP,
        EnumPrLifecyclePhase.POST_MERGE_TAIL,
    ) not in recorded
