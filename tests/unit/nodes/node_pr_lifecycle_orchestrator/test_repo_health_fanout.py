# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for OMN-13586 RH-4: orchestrator fan-out to repo-health classify/repair.

Four transition tests required by the DoD:
  1. repo_baseline  → classify cmd emitted AND repair-start cmd emitted
  2. pr_scoped      → stays in existing fix lane; classify cmd emitted; NO repair-start
  3. unknown        → classify cmd emitted; NO repair-start (surface evidence only)
  4. clean          → no classify cmd; no repair-start (no validation failure)

Related:
    - OMN-13586: RH-4 orchestrator fan-out
    - OMN-13316: Epic — merge-sweep & evidence-automation hardening
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.events.repo_health import EnumFailureOrigin
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

# Topic names that must appear in contract.yaml publish_topics:
TOPIC_REPO_HEALTH_CLASSIFY = "onex.cmd.omnimarket.repo-health-classify.v1"
TOPIC_REPO_HEALTH_REPAIR_START = "onex.cmd.omnimarket.repo-health-repair-start.v1"


# ---------------------------------------------------------------------------
# Test infrastructure (mirrors test_orchestrator_phase_transitions.py style)
# ---------------------------------------------------------------------------


class _RecordingBus:
    """Minimal ProtocolEventBusPublisher stand-in that records publishes."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(self, *, topic: str, key: Any, value: bytes) -> None:
        self.published.append({"topic": topic, "value": value})

    def topics(self) -> list[str]:
        return [p["topic"] for p in self.published]

    def payloads_for(self, topic: str) -> list[dict[str, Any]]:
        import contextlib

        out = []
        for p in self.published:
            if p["topic"] == topic:
                with contextlib.suppress(Exception):
                    out.append(json.loads(p["value"]))
        return out


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
        return MergeResult(prs_merged=0, prs_failed=0)


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

    def _make_merge_queue_adapter(self) -> Any:  # pragma: no cover
        raise AssertionError("merge queue adapter not expected in these tests")

    def _write_result_file(self, run_id: str, result: Any) -> None:
        return None

    def _write_occ_dependency_edges_file(self, run_id: str, edges: Any) -> None:
        return None


def _make_pr(
    pr_number: int,
    *,
    category: EnumPrCategory = EnumPrCategory.RED,
    validation_failure_origin: EnumFailureOrigin | None = None,
) -> tuple[PrRecord, TriageRecord, ReducerIntent]:
    """Build a consistent (PrRecord, TriageRecord, ReducerIntent) triple."""
    repo = "OmniNode-ai/omnimarket"
    pr = PrRecord(pr_number=pr_number, repo=repo, checks_status="failure")
    triage = TriageRecord(
        pr_number=pr_number,
        repo=repo,
        category=category,
        validation_failure_origin=validation_failure_origin,
    )
    intent_kind = (
        EnumReducerIntent.MERGE
        if category == EnumPrCategory.GREEN
        else EnumReducerIntent.FIX
    )
    intent = ReducerIntent(pr_number=pr_number, repo=repo, intent=intent_kind)
    return pr, triage, intent


# ---------------------------------------------------------------------------
# Test 1: repo_baseline → classify cmd + repair-start cmd both emitted
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repo_baseline_emits_classify_and_repair_start() -> None:
    """A fix_pr with validation_failure_origin=REPO_BASELINE must publish both
    repo-health-classify.v1 AND repo-health-repair-start.v1.

    Guardrail (plan §3.3, fact #9/#10): the classify/repair publish must NOT
    become a new hard block on auto-merge arming — the orchestrator continues
    to COMPLETE normally.
    """
    pr, triage, intent = _make_pr(
        401,
        category=EnumPrCategory.RED,
        validation_failure_origin=EnumFailureOrigin.REPO_BASELINE,
    )
    bus = _RecordingBus()
    orch = _Orchestrator(
        _prs=(pr,),
        inventory=_MockInventory((pr,)),
        triage=_MockTriage((triage,)),
        reducer=_MockReducer((intent,)),
        merge=_MockMerge(),
        fix=_MockFix(),
        event_bus=bus,
    )
    result = await orch.handle(
        ModelPrLifecycleStartCommand(
            correlation_id=uuid4(),
            run_id="20260625-test-repo-baseline",
            fix_only=True,
        )
    )

    topics = bus.topics()
    assert TOPIC_REPO_HEALTH_CLASSIFY in topics, (
        f"repo-health-classify.v1 must be published for a REPO_BASELINE failure; "
        f"got topics={topics}"
    )
    assert TOPIC_REPO_HEALTH_REPAIR_START in topics, (
        f"repo-health-repair-start.v1 must be published for a REPO_BASELINE failure; "
        f"got topics={topics}"
    )

    # Payload of classify must carry pr_number and repo
    classify_payloads = bus.payloads_for(TOPIC_REPO_HEALTH_CLASSIFY)
    assert len(classify_payloads) >= 1
    assert classify_payloads[0].get("pr_number") == 401
    assert classify_payloads[0].get("repo") == "OmniNode-ai/omnimarket"
    assert (
        classify_payloads[0].get("failure_origin")
        == EnumFailureOrigin.REPO_BASELINE.value
    )

    # Payload of repair-start must carry pr_number and repo
    repair_payloads = bus.payloads_for(TOPIC_REPO_HEALTH_REPAIR_START)
    assert len(repair_payloads) >= 1
    assert repair_payloads[0].get("pr_number") == 401
    assert repair_payloads[0].get("repo") == "OmniNode-ai/omnimarket"

    # Sweep must still COMPLETE (repo_baseline debt must not hard-block arming)
    assert result.final_state == "COMPLETE"


# ---------------------------------------------------------------------------
# Test 2: pr_scoped → stays in fix lane; classify emitted; NO repair-start
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pr_scoped_stays_in_fix_lane_no_repair_start() -> None:
    """A fix_pr with validation_failure_origin=PR_SCOPED must publish
    repo-health-classify.v1 but NOT repo-health-repair-start.v1.

    The PR stays in the existing fix lane — the fix handler is still called.
    """
    pr, triage, intent = _make_pr(
        402,
        category=EnumPrCategory.RED,
        validation_failure_origin=EnumFailureOrigin.PR_SCOPED,
    )
    bus = _RecordingBus()
    fix_calls: list[Any] = []

    class _CapturingFix:
        async def handle(self, command: Any) -> FixResult:
            fix_calls.append(command)
            return FixResult(prs_dispatched=1, prs_skipped=0)

    orch = _Orchestrator(
        _prs=(pr,),
        inventory=_MockInventory((pr,)),
        triage=_MockTriage((triage,)),
        reducer=_MockReducer((intent,)),
        merge=_MockMerge(),
        fix=_CapturingFix(),
        event_bus=bus,
    )
    result = await orch.handle(
        ModelPrLifecycleStartCommand(
            correlation_id=uuid4(),
            run_id="20260625-test-pr-scoped",
            fix_only=True,
        )
    )

    topics = bus.topics()
    assert TOPIC_REPO_HEALTH_CLASSIFY in topics, (
        f"repo-health-classify.v1 must still be emitted for PR_SCOPED so the "
        f"classify node can record the origin; got topics={topics}"
    )
    assert TOPIC_REPO_HEALTH_REPAIR_START not in topics, (
        f"repo-health-repair-start.v1 must NOT be emitted for PR_SCOPED; "
        f"got topics={topics}"
    )
    # Fix handler was called — PR is still in the fix lane
    assert len(fix_calls) == 1, (
        "Fix handler must still be called for PR_SCOPED failures"
    )
    assert result.final_state == "COMPLETE"


# ---------------------------------------------------------------------------
# Test 3: unknown → classify emitted; NO repair-start
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_origin_emits_classify_no_repair() -> None:
    """A fix_pr with validation_failure_origin=UNKNOWN must publish
    repo-health-classify.v1 but NOT repo-health-repair-start.v1.

    Plan §'Unknowns should not be silently converted' — surface evidence only.
    """
    pr, triage, intent = _make_pr(
        403,
        category=EnumPrCategory.RED,
        validation_failure_origin=EnumFailureOrigin.UNKNOWN,
    )
    bus = _RecordingBus()
    orch = _Orchestrator(
        _prs=(pr,),
        inventory=_MockInventory((pr,)),
        triage=_MockTriage((triage,)),
        reducer=_MockReducer((intent,)),
        merge=_MockMerge(),
        fix=_MockFix(),
        event_bus=bus,
    )
    result = await orch.handle(
        ModelPrLifecycleStartCommand(
            correlation_id=uuid4(),
            run_id="20260625-test-unknown",
            fix_only=True,
        )
    )

    topics = bus.topics()
    assert TOPIC_REPO_HEALTH_CLASSIFY in topics, (
        f"repo-health-classify.v1 must be emitted for UNKNOWN (surface evidence); "
        f"got topics={topics}"
    )
    assert TOPIC_REPO_HEALTH_REPAIR_START not in topics, (
        f"repo-health-repair-start.v1 must NOT be emitted for UNKNOWN; "
        f"got topics={topics}"
    )
    assert result.final_state == "COMPLETE"


# ---------------------------------------------------------------------------
# Test 4: clean PR → no classify cmd; no repair-start
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clean_pr_no_classify_no_repair() -> None:
    """A clean (GREEN) PR must not trigger any repo-health commands.

    No validation failure → no classify cmd → no repair-start cmd.
    """
    repo = "OmniNode-ai/omnimarket"
    pr = PrRecord(
        pr_number=404,
        repo=repo,
        checks_status="success",
    )
    triage = TriageRecord(
        pr_number=404,
        repo=repo,
        category=EnumPrCategory.GREEN,
        validation_failure_origin=None,
    )
    intent = ReducerIntent(
        pr_number=404,
        repo=repo,
        intent=EnumReducerIntent.MERGE,
    )
    bus = _RecordingBus()
    orch = _Orchestrator(
        _prs=(pr,),
        inventory=_MockInventory((pr,)),
        triage=_MockTriage((triage,)),
        reducer=_MockReducer((intent,)),
        merge=_MockMerge(),
        fix=_MockFix(),
        event_bus=bus,
    )
    result = await orch.handle(
        ModelPrLifecycleStartCommand(
            correlation_id=uuid4(),
            run_id="20260625-test-clean",
            merge_only=True,
        )
    )

    topics = bus.topics()
    assert TOPIC_REPO_HEALTH_CLASSIFY not in topics, (
        f"repo-health-classify.v1 must NOT be emitted for a clean PR; "
        f"got topics={topics}"
    )
    assert TOPIC_REPO_HEALTH_REPAIR_START not in topics, (
        f"repo-health-repair-start.v1 must NOT be emitted for a clean PR; "
        f"got topics={topics}"
    )
    assert result.final_state == "COMPLETE"
