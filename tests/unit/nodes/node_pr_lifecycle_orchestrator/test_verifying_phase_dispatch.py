# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""VERIFYING-phase dispatch proof for the PR lifecycle orchestrator (OMN-13673).

These tests drive the real ``HandlerPrLifecycleOrchestrator._run_sweep`` FSM with
mocked sub-handlers and deterministic verification hooks (no live gh / docker).
They prove the wired-up VERIFYING phase:

  * a merge-ready PR whose verification PASSES proceeds to MERGING and merges;
  * a PR whose verification FAILS is left open (not merged) WITHOUT blocking the
    other PRs in the same batch — the batch continues and the sweep COMPLETEs.

Related:
    - OMN-13673: wire verify=True VERIFYING dispatch (real pre-merge verification).
    - OMN-7742 / OMN-8390: per-PR verification target mapping + 7-category outcomes.
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
from omnimarket.nodes.node_pr_lifecycle_orchestrator.verify_target_mapping import (
    EnumVerificationOutcome,
    EnumVerificationTarget,
)

_REPO = "OmniNode-ai/omnimarket"


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
    def __init__(self) -> None:
        self.merged_pr_numbers: list[int] = []

    async def handle(self, command: Any) -> MergeResult:
        self.merged_pr_numbers.append(int(command.pr_number))
        return MergeResult(prs_merged=1, prs_failed=0)


class _MockFix:
    async def handle(self, command: Any) -> FixResult:
        return FixResult(prs_dispatched=1, prs_skipped=0)


class _Orchestrator(HandlerPrLifecycleOrchestrator):
    """Test orchestrator: no gh enumeration, deterministic per-PR verification.

    ``_verification_target_for`` records the PR number being verified (the
    orchestrator processes PRs one at a time), so ``_execute_verification_probe``
    can resolve the injected outcome for that exact PR without touching live
    docker / gh.
    """

    def __init__(
        self,
        *,
        _prs: tuple[PrRecord, ...],
        _outcome_by_pr: dict[int, EnumVerificationOutcome],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._prs = _prs
        self._outcome_by_pr = _outcome_by_pr
        self.probed_pr_numbers: list[int] = []

    def _enumerate_repos(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(pr.repo for pr in self._prs))

    def _enumerate_open_pr_numbers(self, repo: str) -> tuple[int, ...]:
        return tuple(pr.pr_number for pr in self._prs if pr.repo == repo)

    def _make_merge_queue_adapter(self) -> Any:  # pragma: no cover - unused here
        raise AssertionError("merge queue adapter not expected in these tests")

    def _write_result_file(self, run_id: str, result: Any) -> None:
        return None

    def _write_occ_dependency_edges_file(self, run_id: str, edges: Any) -> None:
        return None

    def _verification_target_for(
        self, repo: str, pr_number: int
    ) -> EnumVerificationTarget:
        # Record the PR currently being verified and force a non-skip target so
        # the probe override is exercised.
        self.probed_pr_numbers.append(pr_number)
        return EnumVerificationTarget.RUNTIME_HEALTH

    async def _execute_verification_probe(
        self, *, target: EnumVerificationTarget, timeout_seconds: int
    ) -> EnumVerificationOutcome:
        pr_number = self.probed_pr_numbers[-1]
        return self._outcome_by_pr[pr_number]


def _make_orch(
    *,
    prs: tuple[PrRecord, ...],
    classified: tuple[TriageRecord, ...],
    intents: tuple[ReducerIntent, ...],
    outcome_by_pr: dict[int, EnumVerificationOutcome],
    merge: _MockMerge,
) -> _Orchestrator:
    return _Orchestrator(
        _prs=prs,
        _outcome_by_pr=outcome_by_pr,
        inventory=_MockInventory(prs),
        triage=_MockTriage(classified),
        reducer=_MockReducer(intents),
        merge=merge,
        fix=_MockFix(),
        event_bus=_RecordingBus(),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_merge_ready_pr_passes_verification_and_merges() -> None:
    """A green PR whose verification PASSES proceeds to MERGING and merges."""
    pr = PrRecord(pr_number=401, repo=_REPO, checks_status="success")
    triage = TriageRecord(pr_number=401, repo=_REPO, category=EnumPrCategory.GREEN)
    intent = ReducerIntent(pr_number=401, repo=_REPO, intent=EnumReducerIntent.MERGE)
    merge = _MockMerge()
    orch = _make_orch(
        prs=(pr,),
        classified=(triage,),
        intents=(intent,),
        outcome_by_pr={401: EnumVerificationOutcome.MERGED},
        merge=merge,
    )

    result = await orch.handle(
        ModelPrLifecycleStartCommand(
            correlation_id=uuid4(),
            run_id="20260627-verify-pass",
            merge_only=True,
            verify=True,
        )
    )

    assert result.final_state == "COMPLETE"
    assert merge.merged_pr_numbers == [401]
    assert result.prs_verified == 1
    assert result.prs_verification_blocked == 0
    assert result.verification_breakdown["MERGED"] == 1
    # A verification-completed event was published for the PR.
    assert any(
        "verification-completed" in pub["topic"]
        for pub in orch._event_bus.published  # type: ignore[attr-defined]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_pr_verification_failure_does_not_block_batch() -> None:
    """A VERIFICATION_FAILED PR stays open; the other PR still merges.

    Proves per-PR isolation: one PR's verification failure removes only that PR
    from the merge set and never aborts the batch or fails the sweep.
    """
    pr_pass = PrRecord(pr_number=501, repo=_REPO, checks_status="success")
    pr_fail = PrRecord(pr_number=502, repo=_REPO, checks_status="success")
    classified = (
        TriageRecord(pr_number=501, repo=_REPO, category=EnumPrCategory.GREEN),
        TriageRecord(pr_number=502, repo=_REPO, category=EnumPrCategory.GREEN),
    )
    intents = (
        ReducerIntent(pr_number=501, repo=_REPO, intent=EnumReducerIntent.MERGE),
        ReducerIntent(pr_number=502, repo=_REPO, intent=EnumReducerIntent.MERGE),
    )
    merge = _MockMerge()
    orch = _make_orch(
        prs=(pr_pass, pr_fail),
        classified=classified,
        intents=intents,
        outcome_by_pr={
            501: EnumVerificationOutcome.MERGED,
            502: EnumVerificationOutcome.VERIFICATION_FAILED,
        },
        merge=merge,
    )

    result = await orch.handle(
        ModelPrLifecycleStartCommand(
            correlation_id=uuid4(),
            run_id="20260627-verify-mixed",
            merge_only=True,
            verify=True,
        )
    )

    assert result.final_state == "COMPLETE"
    # Only the passing PR merged; the failed PR stayed open.
    assert merge.merged_pr_numbers == [501]
    assert result.prs_verified == 1
    assert result.prs_verification_blocked == 1
    assert result.verification_breakdown["MERGED"] == 1
    assert result.verification_breakdown["VERIFICATION_FAILED"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_neutral_outcomes_proceed_to_merge() -> None:
    """Non-MERGED, non-FAILED outcomes are NEUTRAL skips that still merge."""
    pr = PrRecord(pr_number=601, repo=_REPO, checks_status="success")
    triage = TriageRecord(pr_number=601, repo=_REPO, category=EnumPrCategory.GREEN)
    intent = ReducerIntent(pr_number=601, repo=_REPO, intent=EnumReducerIntent.MERGE)
    merge = _MockMerge()
    orch = _make_orch(
        prs=(pr,),
        classified=(triage,),
        intents=(intent,),
        outcome_by_pr={601: EnumVerificationOutcome.VERIFICATION_UNAVAILABLE},
        merge=merge,
    )

    result = await orch.handle(
        ModelPrLifecycleStartCommand(
            correlation_id=uuid4(),
            run_id="20260627-verify-neutral",
            merge_only=True,
            verify=True,
        )
    )

    assert result.final_state == "COMPLETE"
    # Neutral outcome → proceeds to merge, not counted as verified or blocked.
    assert merge.merged_pr_numbers == [601]
    assert result.prs_verified == 0
    assert result.prs_verification_blocked == 0
    assert result.verification_breakdown["VERIFICATION_UNAVAILABLE"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_verify_renders_breakdown_without_raising() -> None:
    """verify=True dry_run classifies every PR SKIPPED_BY_POLICY and never raises."""
    pr = PrRecord(pr_number=701, repo=_REPO, checks_status="success")
    triage = TriageRecord(pr_number=701, repo=_REPO, category=EnumPrCategory.GREEN)
    intent = ReducerIntent(pr_number=701, repo=_REPO, intent=EnumReducerIntent.MERGE)
    merge = _MockMerge()
    orch = _make_orch(
        prs=(pr,),
        classified=(triage,),
        intents=(intent,),
        outcome_by_pr={701: EnumVerificationOutcome.MERGED},
        merge=merge,
    )

    result = await orch.handle(
        ModelPrLifecycleStartCommand(
            correlation_id=uuid4(),
            run_id="20260627-verify-dryrun",
            verify=True,
            dry_run=True,
        )
    )

    assert result.final_state == "COMPLETE"
    # dry_run executes no real probe and merges nothing.
    assert merge.merged_pr_numbers == []
    assert result.verification_breakdown["SKIPPED_BY_POLICY"] == 1
    # All 7 categories are present in the rendered breakdown.
    assert set(result.verification_breakdown) == {
        "MERGED",
        "VERIFICATION_FAILED",
        "VERIFICATION_UNAVAILABLE",
        "VERIFICATION_TIMEOUT",
        "VERIFICATION_TOOL_ERROR",
        "SKIPPED_NO_MAPPING",
        "SKIPPED_BY_POLICY",
    }
