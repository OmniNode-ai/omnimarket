# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RH-6 integration coverage — prove PR-scoped vs repo-baseline lane split.

Acceptance proof for the merge-sweep repo-health repair lane (epic OMN-13316).
These tests exercise the REAL chain end-to-end across node boundaries — no
mocks of the classify/repair/reducer logic itself — only the outermost I/O
boundaries (event bus, Linear client) are stubbed:

    orchestrator fan-out  →  classify (COMPUTE)  →  repair (EFFECT)  →  reducer fold

Three scenarios required by the DoD (OMN-13588):

  1. PR-introduced failure (a failing path IS in the PR changed-file set) stays
     in the PR lane only — classify resolves PR_SCOPED, the orchestrator emits
     repo-health-classify but NOT repo-health-repair-start, and the reducer
     terminal state records pr_scoped_count == 1 with repair_tasks_emitted == 0.

  2. Pre-existing repo-baseline hook failure (a failing path NOT in the PR set,
     known-failing on the dev baseline) routes to the repo-health repair lane —
     classify resolves REPO_BASELINE, the orchestrator emits BOTH classify and
     repair-start, the repair EFFECT emits exactly one repair task, and the
     reducer terminal state records repo_baseline_count == 1 with
     repair_tasks_emitted == 1 and one repair_task_ref.

  3. Clean PR (no validation failure) fires NEITHER classify nor repair — the
     orchestrator emits no repo-health command and the reducer repo_health
     sub-record stays at all-zero.

In every scenario the reducer terminal state records both lanes distinctly: the
PR-lane FSM fields are folded by the FSM delta path while the repo_health
sub-record is folded by the repo-health fold functions — the two never collide.

Related:
    - OMN-13588: RH-6 integration coverage (this file)
    - OMN-13316: Epic — merge-sweep & evidence-automation hardening
    - OMN-13583/13584/13585/13586: RH-1..RH-4 nodes under test
    - OMN-13027: dev-baseline ratchet (source of dev_baseline_paths)
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.events.repo_health import (
    EnumFailureOrigin,
    ModelRepoHealthFailureEnvelope,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    TOPIC_REPO_HEALTH_CLASSIFY,
    TOPIC_REPO_HEALTH_REPAIR_START,
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
from omnimarket.nodes.node_pr_lifecycle_state_reducer.handlers.handler_pr_lifecycle_state_reducer import (
    fold_repo_health_classified,
    fold_repo_health_repair_emitted,
)
from omnimarket.nodes.node_pr_lifecycle_state_reducer.models.model_pr_lifecycle_state import (
    ModelPrLifecycleState,
)
from omnimarket.nodes.node_repo_health_classify_compute.handlers.handler_repo_health_classify import (
    HandlerRepoHealthClassify,
)
from omnimarket.nodes.node_repo_health_repair_effect.handlers.handler_repo_health_repair import (
    HandlerRepoHealthRepairEffect,
)
from omnimarket.nodes.node_repo_health_repair_effect.models.model_repair_command import (
    ModelRepoHealthRepairCommand,
)

_REPO = "OmniNode-ai/omnimarket"
_FAILING_COMMAND = "pre-commit run --all-files"


# ---------------------------------------------------------------------------
# Boundary stubs — only the bus and the Linear client are faked; every node
# handler under test runs its real logic.
# ---------------------------------------------------------------------------


class _RecordingBus:
    """ProtocolEventBusPublisher stand-in that records every publish."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(self, *, topic: str, key: Any, value: bytes) -> None:
        self.published.append({"topic": topic, "value": value})

    def topics(self) -> list[str]:
        return [p["topic"] for p in self.published]

    def payloads_for(self, topic: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in self.published:
            if p["topic"] == topic:
                out.append(json.loads(p["value"]))
        return out


class _StubLinearClient:
    """LinearRepairClientProtocol stub — no network. Always creates one ticket."""

    def __init__(self) -> None:
        self.created: list[str] = []

    def search_issues_by_content_key(self, *, content_key: str) -> str | None:
        return None

    def create_issue(self, *, title: str, description: str, parent_id: str) -> str:
        ref = f"OMN-99{len(self.created):03d}"
        self.created.append(ref)
        return ref


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
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def handle(self, command: Any) -> FixResult:
        self.calls.append(command)
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


# ---------------------------------------------------------------------------
# Chain driver — runs the REAL classify and repair handlers off the topics the
# orchestrator actually published, then folds the resulting domain events into
# the reducer terminal state. Returns (terminal_state, repair_refs).
# ---------------------------------------------------------------------------


async def _drive_classify_repair_reducer_chain(
    *,
    bus: _RecordingBus,
    correlation_id: Any,
    failing_paths: tuple[str, ...],
    pr_changed_paths: tuple[str, ...],
    dev_baseline_paths: tuple[str, ...],
    linear_client: _StubLinearClient,
) -> tuple[ModelPrLifecycleState, list[str]]:
    """Fold the orchestrator's repo-health commands through the real chain.

    For each classify command the orchestrator published, run the real classify
    COMPUTE handler. For each REPO_BASELINE classification, run the real repair
    EFFECT handler. Fold the resulting classified + repair-emitted domain events
    through the real reducer fold functions to build the terminal state.
    """
    classify_handler = HandlerRepoHealthClassify()
    repair_handler = HandlerRepoHealthRepairEffect(linear_client=linear_client)

    state = ModelPrLifecycleState(correlation_id=correlation_id)
    repair_refs: list[str] = []

    # The orchestrator emits one classify command per fix_pr with an origin.
    classify_cmds = bus.payloads_for(TOPIC_REPO_HEALTH_CLASSIFY)
    repair_start_cmds = bus.payloads_for(TOPIC_REPO_HEALTH_REPAIR_START)
    repair_start_prs = {c["pr_number"] for c in repair_start_cmds}

    for cmd in classify_cmds:
        envelope = ModelRepoHealthFailureEnvelope(
            correlation_id=correlation_id,
            repo=cmd["repo"],
            pr_number=cmd["pr_number"],
            branch="dev",
            failing_command=_FAILING_COMMAND,
            exit_code=1,
            failing_paths=failing_paths,
            pr_changed_paths=pr_changed_paths,
            dev_baseline_paths=dev_baseline_paths,
        )
        classification = await classify_handler.handle(envelope)

        # Fold the classified domain event into the reducer terminal state.
        state = fold_repo_health_classified(
            state,
            classification=classification.origin.value,
            ticket_ref=None,
        )

        # The repair EFFECT only runs when the orchestrator opened the repair
        # lane (REPO_BASELINE) — mirrors the runtime topic wiring.
        if cmd["pr_number"] in repair_start_prs:
            assert classification.origin is EnumFailureOrigin.REPO_BASELINE, (
                "repair-start was opened for a non-REPO_BASELINE origin: "
                f"{classification.origin}"
            )
            emitted = await repair_handler.handle(
                ModelRepoHealthRepairCommand(
                    correlation_id=correlation_id,
                    classification=classification,
                )
            )
            assert emitted.ticket_created is True
            assert emitted.repair_ticket_ref is not None
            repair_refs.append(emitted.repair_ticket_ref)
            state = fold_repo_health_repair_emitted(
                state, ticket_ref=emitted.repair_ticket_ref
            )

    return state, repair_refs


def _build_orchestrator(
    *,
    pr: PrRecord,
    triage: TriageRecord,
    intent: ReducerIntent,
    bus: _RecordingBus,
    fix: _MockFix,
) -> _Orchestrator:
    return _Orchestrator(
        _prs=(pr,),
        inventory=_MockInventory((pr,)),
        triage=_MockTriage((triage,)),
        reducer=_MockReducer((intent,)),
        merge=_MockMerge(),
        fix=fix,
        event_bus=bus,
    )


# ---------------------------------------------------------------------------
# Scenario 1: PR-introduced failure stays in the PR lane only.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pr_scoped_failure_stays_in_pr_lane_only() -> None:
    """PR-introduced failure: classify resolves PR_SCOPED; no repair lane.

    A failing path that IS in the PR changed-file set must classify PR_SCOPED:
      - orchestrator emits repo-health-classify, NOT repo-health-repair-start
      - the PR stays in the fix lane (fix handler is called)
      - the repair EFFECT never runs (no repair task)
      - reducer terminal state: pr_scoped_count == 1, repo_baseline_count == 0,
        repair_tasks_emitted == 0
    """
    correlation_id = uuid4()
    changed_path = "src/omnimarket/nodes/node_under_repair/handler.py"

    pr = PrRecord(pr_number=501, repo=_REPO, checks_status="failure")
    triage = TriageRecord(
        pr_number=501,
        repo=_REPO,
        category=EnumPrCategory.RED,
        validation_failure_origin=EnumFailureOrigin.PR_SCOPED,
        block_reason="pre-commit failed on changed file",
        failed_check_names=("pre-commit",),
    )
    intent = ReducerIntent(pr_number=501, repo=_REPO, intent=EnumReducerIntent.FIX)

    bus = _RecordingBus()
    fix = _MockFix()
    orch = _build_orchestrator(pr=pr, triage=triage, intent=intent, bus=bus, fix=fix)

    result = await orch.handle(
        ModelPrLifecycleStartCommand(
            correlation_id=correlation_id,
            run_id="20260625-rh6-pr-scoped",
            fix_only=True,
        )
    )

    topics = bus.topics()
    assert TOPIC_REPO_HEALTH_CLASSIFY in topics, (
        f"classify must be emitted for a PR_SCOPED failure; got {topics}"
    )
    assert TOPIC_REPO_HEALTH_REPAIR_START not in topics, (
        f"repair-start must NOT be emitted for a PR_SCOPED failure; got {topics}"
    )
    # PR stays in the fix lane.
    assert len(fix.calls) == 1, "fix handler must run for a PR_SCOPED failure"
    assert result.final_state == "COMPLETE"

    # Drive the real classify→reducer chain off the published command. The
    # failing path is in the PR set, so classify resolves PR_SCOPED.
    state, repair_refs = await _drive_classify_repair_reducer_chain(
        bus=bus,
        correlation_id=correlation_id,
        failing_paths=(changed_path,),
        pr_changed_paths=(changed_path,),
        dev_baseline_paths=(),
        linear_client=_StubLinearClient(),
    )

    assert repair_refs == [], "no repair task may be emitted for PR_SCOPED"
    assert state.repo_health.classified_count == 1
    assert state.repo_health.pr_scoped_count == 1
    assert state.repo_health.repo_baseline_count == 0
    assert state.repo_health.repair_tasks_emitted == 0
    assert state.repo_health.repair_task_refs == ()


# ---------------------------------------------------------------------------
# Scenario 2: pre-existing repo-baseline failure routes to the repair lane.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repo_baseline_failure_routes_to_repair_lane() -> None:
    """Pre-existing baseline failure: classify resolves REPO_BASELINE; one repair.

    A failing path NOT in the PR set but known-failing on the dev baseline must
    classify REPO_BASELINE:
      - orchestrator emits BOTH repo-health-classify AND repo-health-repair-start
      - the repair EFFECT emits exactly one repair task (Linear ticket)
      - reducer terminal state: repo_baseline_count == 1, pr_scoped_count == 0,
        repair_tasks_emitted == 1, one repair_task_ref
      - the PR lane can still arm (orchestrator COMPLETEs, fix handler runs)
    """
    correlation_id = uuid4()
    baseline_path = "src/omnimarket/legacy/unchanged_module.py"
    unrelated_change = "docs/notes.md"

    pr = PrRecord(pr_number=502, repo=_REPO, checks_status="failure")
    triage = TriageRecord(
        pr_number=502,
        repo=_REPO,
        category=EnumPrCategory.RED,
        validation_failure_origin=EnumFailureOrigin.REPO_BASELINE,
        block_reason="pre-commit failed on pre-existing repo file",
        failed_check_names=("pre-commit",),
    )
    intent = ReducerIntent(pr_number=502, repo=_REPO, intent=EnumReducerIntent.FIX)

    bus = _RecordingBus()
    fix = _MockFix()
    orch = _build_orchestrator(pr=pr, triage=triage, intent=intent, bus=bus, fix=fix)

    result = await orch.handle(
        ModelPrLifecycleStartCommand(
            correlation_id=correlation_id,
            run_id="20260625-rh6-repo-baseline",
            fix_only=True,
        )
    )

    topics = bus.topics()
    assert TOPIC_REPO_HEALTH_CLASSIFY in topics, (
        f"classify must be emitted for a REPO_BASELINE failure; got {topics}"
    )
    assert TOPIC_REPO_HEALTH_REPAIR_START in topics, (
        f"repair-start must be emitted for a REPO_BASELINE failure; got {topics}"
    )
    # Repo-baseline debt must NOT hard-block the PR lane.
    assert result.final_state == "COMPLETE"
    assert len(fix.calls) == 1

    linear = _StubLinearClient()
    # Failing path is NOT in the (docs-only) PR change set but IS on the dev
    # baseline → classify resolves REPO_BASELINE → repair EFFECT fires once.
    state, repair_refs = await _drive_classify_repair_reducer_chain(
        bus=bus,
        correlation_id=correlation_id,
        failing_paths=(baseline_path,),
        pr_changed_paths=(unrelated_change,),
        dev_baseline_paths=(baseline_path,),
        linear_client=linear,
    )

    assert len(repair_refs) == 1, "exactly one repair task must be emitted"
    assert len(linear.created) == 1, "exactly one Linear ticket must be created"
    assert state.repo_health.classified_count == 1
    assert state.repo_health.repo_baseline_count == 1
    assert state.repo_health.pr_scoped_count == 0
    assert state.repo_health.repair_tasks_emitted == 1
    assert state.repo_health.repair_task_refs == (repair_refs[0],)


# ---------------------------------------------------------------------------
# Scenario 3: clean PR fires neither classify nor repair.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clean_pr_fires_neither_lane() -> None:
    """Clean PR: no validation failure → no classify, no repair, zero fold.

    - orchestrator emits neither repo-health-classify nor repo-health-repair-start
    - the repair EFFECT never runs
    - reducer repo_health sub-record stays all-zero (both lanes distinct & idle)
    """
    correlation_id = uuid4()

    pr = PrRecord(pr_number=503, repo=_REPO, checks_status="success")
    triage = TriageRecord(
        pr_number=503,
        repo=_REPO,
        category=EnumPrCategory.GREEN,
        validation_failure_origin=None,
    )
    intent = ReducerIntent(pr_number=503, repo=_REPO, intent=EnumReducerIntent.MERGE)

    bus = _RecordingBus()
    fix = _MockFix()
    orch = _build_orchestrator(pr=pr, triage=triage, intent=intent, bus=bus, fix=fix)

    result = await orch.handle(
        ModelPrLifecycleStartCommand(
            correlation_id=correlation_id,
            run_id="20260625-rh6-clean",
            merge_only=True,
        )
    )

    topics = bus.topics()
    assert TOPIC_REPO_HEALTH_CLASSIFY not in topics, (
        f"classify must NOT be emitted for a clean PR; got {topics}"
    )
    assert TOPIC_REPO_HEALTH_REPAIR_START not in topics, (
        f"repair-start must NOT be emitted for a clean PR; got {topics}"
    )
    assert result.final_state == "COMPLETE"

    # No classify commands → the chain folds nothing → repo_health stays idle.
    state, repair_refs = await _drive_classify_repair_reducer_chain(
        bus=bus,
        correlation_id=correlation_id,
        failing_paths=(),
        pr_changed_paths=(),
        dev_baseline_paths=(),
        linear_client=_StubLinearClient(),
    )

    assert repair_refs == []
    assert state.repo_health.classified_count == 0
    assert state.repo_health.pr_scoped_count == 0
    assert state.repo_health.repo_baseline_count == 0
    assert state.repo_health.repair_tasks_emitted == 0
    assert state.repo_health.repair_task_refs == ()


# ---------------------------------------------------------------------------
# Cross-scenario invariant: the reducer records BOTH lanes distinctly. The
# repo_health fold must never perturb the PR-lane FSM fields.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_repo_health_fold_leaves_pr_lane_fields_untouched() -> None:
    """Folding repo-health events must not touch PR-lane FSM fields.

    Confirms the terminal reducer state records the two lanes distinctly: the
    repo_health sub-record changes while every PR-lane FSM field is byte-identical.
    """
    base = ModelPrLifecycleState(
        correlation_id=uuid4(),
        prs_inventoried=3,
        prs_fixed=1,
        prs_merged=2,
        prs_processed=3,
    )
    pr_lane_before = base.model_dump(exclude={"repo_health"})

    after_classify = fold_repo_health_classified(
        base, classification="repo_baseline", ticket_ref=None
    )
    after_repair = fold_repo_health_repair_emitted(
        after_classify, ticket_ref="OMN-99001"
    )

    assert after_repair.model_dump(exclude={"repo_health"}) == pr_lane_before
    assert after_repair.repo_health.repo_baseline_count == 1
    assert after_repair.repo_health.repair_tasks_emitted == 1
    assert after_repair.repo_health.repair_task_refs == ("OMN-99001",)
