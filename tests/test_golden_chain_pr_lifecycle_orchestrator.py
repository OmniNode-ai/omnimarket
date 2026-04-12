"""Golden chain tests for node_pr_lifecycle_orchestrator.

Verifies the FSM orchestrator composes 5 sub-handlers via mock adapters:
  start command -> phase transitions -> completion.
Uses EventBusInmemory, zero infra required.

Related:
    - OMN-8087: Create pr_lifecycle_orchestrator Node
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    HandlerPrLifecycleOrchestrator,
    ModelPrLifecycleResult,
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

TOPIC_PHASE_TRANSITION = (
    "onex.evt.omnimarket.pr-lifecycle-orchestrator-phase-transition.v1"
)


# ---------------------------------------------------------------------------
# Mock sub-handlers
# ---------------------------------------------------------------------------


class MockInventory:
    def __init__(self, prs: tuple[PrRecord, ...] = ()) -> None:
        self._prs = prs
        self.call_count = 0
        self.last_repos: tuple[str, ...] = ()
        self.last_dry_run: bool = False

    async def handle(
        self,
        *,
        correlation_id: UUID,
        repos: tuple[str, ...] = (),
        dry_run: bool = False,
    ) -> InventoryResult:
        self.call_count += 1
        self.last_repos = repos
        self.last_dry_run = dry_run
        return InventoryResult(prs=self._prs, total_collected=len(self._prs))


class MockTriage:
    def __init__(self, classified: tuple[TriageRecord, ...] = ()) -> None:
        self._classified = classified
        self.call_count = 0

    async def handle(
        self,
        *,
        correlation_id: UUID,
        prs: tuple[PrRecord, ...],
    ) -> PrTriageResult:
        self.call_count += 1
        green = sum(1 for r in self._classified if r.category == EnumPrCategory.GREEN)
        non_green = len(self._classified) - green
        return PrTriageResult(
            classified=self._classified,
            green_count=green,
            non_green_count=non_green,
        )


class MockReducer:
    def __init__(self, intents: tuple[ReducerIntent, ...] = ()) -> None:
        self._intents = intents
        self.call_count = 0
        self.last_dry_run: bool = False
        self.last_fix_only: bool = False
        self.last_merge_only: bool = False

    async def handle(
        self,
        *,
        correlation_id: UUID,
        classified: tuple[TriageRecord, ...],
        dry_run: bool = False,
        inventory_only: bool = False,
        fix_only: bool = False,
        merge_only: bool = False,
    ) -> ReducerResult:
        self.call_count += 1
        self.last_dry_run = dry_run
        self.last_fix_only = fix_only
        self.last_merge_only = merge_only
        merge_count = sum(
            1 for i in self._intents if i.intent == EnumReducerIntent.MERGE
        )
        fix_count = sum(1 for i in self._intents if i.intent == EnumReducerIntent.FIX)
        skip_count = sum(1 for i in self._intents if i.intent == EnumReducerIntent.SKIP)
        return ReducerResult(
            intents=self._intents,
            merge_count=merge_count,
            fix_count=fix_count,
            skip_count=skip_count,
        )


class MockMerge:
    def __init__(self, *, prs_merged: int = 0, fail: bool = False) -> None:
        self._prs_merged = prs_merged
        self._fail = fail
        self.call_count = 0
        self.last_dry_run: bool = False

    async def handle(
        self,
        *,
        correlation_id: UUID,
        prs_to_merge: tuple[TriageRecord, ...],
        dry_run: bool = False,
    ) -> MergeResult:
        self.call_count += 1
        self.last_dry_run = dry_run
        if self._fail:
            msg = "merge failed"
            raise RuntimeError(msg)
        return MergeResult(prs_merged=self._prs_merged, prs_failed=0)


class MockFix:
    def __init__(
        self, *, prs_dispatched: int | None = None, fail: bool = False
    ) -> None:
        self._prs_dispatched = prs_dispatched  # None = use len(prs_to_fix)
        self._fail = fail
        self.call_count = 0
        self.last_dry_run: bool = False
        self.dispatched_pr_numbers: list[int] = []
        self._in_flight = 0
        self.max_in_flight = 0

    async def handle(
        self,
        *,
        correlation_id: UUID,
        prs_to_fix: tuple[TriageRecord, ...],
        dry_run: bool = False,
    ) -> FixResult:
        import asyncio

        self._in_flight += 1
        if self._in_flight > self.max_in_flight:
            self.max_in_flight = self._in_flight
        try:
            await asyncio.sleep(0)  # yield to allow concurrent tasks to enter
            self.call_count += 1
            self.last_dry_run = dry_run
            self.dispatched_pr_numbers.extend(pr.pr_number for pr in prs_to_fix)
            if self._fail:
                msg = "fix failed"
                raise RuntimeError(msg)
            count = (
                self._prs_dispatched
                if self._prs_dispatched is not None
                else len(prs_to_fix)
            )
            return FixResult(prs_dispatched=count, prs_skipped=0)
        finally:
            self._in_flight -= 1


# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------

_PR_GREEN = PrRecord(
    pr_number=101,
    repo="OmniNode-ai/omnimarket",
    checks_status="success",
    review_status="approved",
)
_PR_RED = PrRecord(
    pr_number=102,
    repo="OmniNode-ai/omnimarket",
    checks_status="failure",
    review_status="pending",
)
_TRIAGE_GREEN = TriageRecord(
    pr_number=101, repo="OmniNode-ai/omnimarket", category=EnumPrCategory.GREEN
)
_TRIAGE_RED = TriageRecord(
    pr_number=102, repo="OmniNode-ai/omnimarket", category=EnumPrCategory.RED
)
_INTENT_MERGE = ReducerIntent(
    pr_number=101, repo="OmniNode-ai/omnimarket", intent=EnumReducerIntent.MERGE
)
_INTENT_FIX = ReducerIntent(
    pr_number=102, repo="OmniNode-ai/omnimarket", intent=EnumReducerIntent.FIX
)


def _make_command(**kwargs: object) -> ModelPrLifecycleStartCommand:
    defaults: dict[str, object] = {
        "correlation_id": uuid4(),
        "run_id": "20260411-000000-test01",
    }
    defaults.update(kwargs)
    return ModelPrLifecycleStartCommand(**defaults)  # type: ignore[arg-type]


def _make_orchestrator(
    *,
    inventory: MockInventory | None = None,
    triage: MockTriage | None = None,
    reducer: MockReducer | None = None,
    merge: MockMerge | None = None,
    fix: MockFix | None = None,
    event_bus: EventBusInmemory | None = None,
) -> HandlerPrLifecycleOrchestrator:
    return HandlerPrLifecycleOrchestrator(
        inventory=inventory or MockInventory(),
        triage=triage or MockTriage(),
        reducer=reducer or MockReducer(),
        merge=merge or MockMerge(),
        fix=fix or MockFix(),
        event_bus=event_bus,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPrLifecycleOrchestratorGoldenChain:
    """Golden chain: orchestrator composes sub-handlers through FSM states."""

    async def test_empty_inventory_completes_cleanly(self) -> None:
        """Zero PRs in inventory -> COMPLETE with all counts zero."""
        orch = _make_orchestrator(inventory=MockInventory(prs=()))
        result = await orch.handle(_make_command())

        assert isinstance(result, ModelPrLifecycleResult)
        assert result.final_state == "COMPLETE"
        assert result.prs_inventoried == 0
        assert result.prs_merged == 0
        assert result.prs_fixed == 0

    async def test_full_pipeline_merge_and_fix(self) -> None:
        """One green PR merged, one red PR fixed -> COMPLETE with correct counts."""
        inventory = MockInventory(prs=(_PR_GREEN, _PR_RED))
        triage = MockTriage(classified=(_TRIAGE_GREEN, _TRIAGE_RED))
        reducer = MockReducer(intents=(_INTENT_MERGE, _INTENT_FIX))
        merge = MockMerge(prs_merged=1)
        fix = MockFix(prs_dispatched=1)

        orch = _make_orchestrator(
            inventory=inventory,
            triage=triage,
            reducer=reducer,
            merge=merge,
            fix=fix,
        )
        result = await orch.handle(_make_command())

        assert result.final_state == "COMPLETE"
        assert result.prs_inventoried == 2
        assert result.prs_merged == 1
        assert result.prs_fixed == 1
        assert inventory.call_count == 1
        assert triage.call_count == 1
        assert reducer.call_count == 1
        assert merge.call_count == 1
        assert fix.call_count == 1

    async def test_inventory_only_flag(self) -> None:
        """inventory_only=True -> triage/reduce/merge/fix handlers NOT called."""
        inventory = MockInventory(prs=(_PR_GREEN,))
        triage = MockTriage()
        reducer = MockReducer()
        merge = MockMerge()
        fix = MockFix()

        orch = _make_orchestrator(
            inventory=inventory,
            triage=triage,
            reducer=reducer,
            merge=merge,
            fix=fix,
        )
        result = await orch.handle(_make_command(inventory_only=True))

        assert result.final_state == "COMPLETE"
        assert result.prs_inventoried == 1
        assert triage.call_count == 0
        assert reducer.call_count == 0
        assert merge.call_count == 0
        assert fix.call_count == 0

    async def test_dry_run_skips_merge_and_fix(self) -> None:
        """dry_run=True -> reducer called but merge/fix NOT called."""
        inventory = MockInventory(prs=(_PR_GREEN, _PR_RED))
        triage = MockTriage(classified=(_TRIAGE_GREEN, _TRIAGE_RED))
        reducer = MockReducer(intents=(_INTENT_MERGE, _INTENT_FIX))
        merge = MockMerge()
        fix = MockFix()

        orch = _make_orchestrator(
            inventory=inventory,
            triage=triage,
            reducer=reducer,
            merge=merge,
            fix=fix,
        )
        result = await orch.handle(_make_command(dry_run=True))

        assert result.final_state == "COMPLETE"
        assert result.prs_inventoried == 2
        assert reducer.call_count == 1
        assert reducer.last_dry_run is True
        assert merge.call_count == 0
        assert fix.call_count == 0
        # All intents are recorded as skipped
        assert result.prs_skipped == 2

    async def test_merge_only_flag(self) -> None:
        """merge_only=True -> fix handler NOT called after merge."""
        inventory = MockInventory(prs=(_PR_GREEN, _PR_RED))
        triage = MockTriage(classified=(_TRIAGE_GREEN, _TRIAGE_RED))
        reducer = MockReducer(intents=(_INTENT_MERGE, _INTENT_FIX))
        merge = MockMerge(prs_merged=1)
        fix = MockFix()

        orch = _make_orchestrator(
            inventory=inventory,
            triage=triage,
            reducer=reducer,
            merge=merge,
            fix=fix,
        )
        result = await orch.handle(_make_command(merge_only=True))

        assert result.final_state == "COMPLETE"
        assert result.prs_merged == 1
        assert merge.call_count == 1
        assert fix.call_count == 0

    async def test_fix_only_flag(self) -> None:
        """fix_only=True -> merge handler NOT called; fix IS called."""
        inventory = MockInventory(prs=(_PR_GREEN, _PR_RED))
        triage = MockTriage(classified=(_TRIAGE_GREEN, _TRIAGE_RED))
        reducer = MockReducer(intents=(_INTENT_MERGE, _INTENT_FIX))
        merge = MockMerge()
        fix = MockFix(prs_dispatched=1)

        orch = _make_orchestrator(
            inventory=inventory,
            triage=triage,
            reducer=reducer,
            merge=merge,
            fix=fix,
        )
        result = await orch.handle(_make_command(fix_only=True))

        assert result.final_state == "COMPLETE"
        assert result.prs_fixed == 1
        assert merge.call_count == 0
        assert fix.call_count == 1

    async def test_repos_filter_propagated_to_inventory(self) -> None:
        """repos CSV filter is passed through to the inventory handler."""
        inventory = MockInventory()
        orch = _make_orchestrator(inventory=inventory)

        await orch.handle(
            _make_command(repos="OmniNode-ai/omnimarket,OmniNode-ai/omniclaude")
        )

        assert inventory.last_repos == (
            "OmniNode-ai/omnimarket",
            "OmniNode-ai/omniclaude",
        )

    async def test_exception_in_inventory_leads_to_failed_state(self) -> None:
        """Exception in inventory -> final_state=FAILED with error_message set."""

        class BrokenInventory:
            async def handle(
                self,
                *,
                correlation_id: UUID,
                repos: tuple[str, ...] = (),
                dry_run: bool = False,
            ) -> InventoryResult:
                msg = "GitHub API down"
                raise RuntimeError(msg)

        orch = _make_orchestrator(inventory=BrokenInventory())  # type: ignore[arg-type]
        result = await orch.handle(_make_command())

        assert result.final_state == "FAILED"
        assert result.error_message is not None
        assert "GitHub API down" in result.error_message

    async def test_exception_in_merge_leads_to_failed_state(self) -> None:
        """Exception in merge handler -> final_state=FAILED."""
        inventory = MockInventory(prs=(_PR_GREEN,))
        triage = MockTriage(classified=(_TRIAGE_GREEN,))
        reducer = MockReducer(intents=(_INTENT_MERGE,))
        merge = MockMerge(fail=True)

        orch = _make_orchestrator(
            inventory=inventory,
            triage=triage,
            reducer=reducer,
            merge=merge,
        )
        result = await orch.handle(_make_command())

        assert result.final_state == "FAILED"
        assert result.error_message is not None

    async def test_event_bus_receives_phase_transitions(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Phase transitions are published as events to the bus."""
        await event_bus.start()

        inventory = MockInventory(prs=(_PR_GREEN,))
        triage = MockTriage(classified=(_TRIAGE_GREEN,))
        reducer = MockReducer(intents=(_INTENT_MERGE,))
        merge = MockMerge(prs_merged=1)

        orch = _make_orchestrator(
            inventory=inventory,
            triage=triage,
            reducer=reducer,
            merge=merge,
            event_bus=event_bus,
        )
        result = await orch.handle(_make_command())

        assert result.final_state == "COMPLETE"

        history = await event_bus.get_event_history(topic=TOPIC_PHASE_TRANSITION)
        # Transitions: IDLE->INVENTORYING, INVENTORYING->TRIAGING, TRIAGING->MERGING, MERGING->COMPLETE
        assert len(history) >= 3

        first_payload = json.loads(history[0].value)
        assert first_payload["from_phase"] == "idle"
        assert first_payload["to_phase"] == "inventorying"

        await event_bus.close()

    def test_zero_arg_construction_succeeds(self) -> None:
        """Auto-wiring runtime can construct orchestrator with zero args."""
        orch = HandlerPrLifecycleOrchestrator()
        assert orch._inventory is None
        assert orch._triage is None
        assert orch._reducer is None
        assert orch._merge is None
        assert orch._fix is None

    def test_explicit_injection_preserves_references(self) -> None:
        """Sub-handlers passed explicitly are stored and retrievable."""
        inventory = MockInventory()
        triage = MockTriage()
        reducer = MockReducer()
        merge = MockMerge()
        fix = MockFix()

        orch = HandlerPrLifecycleOrchestrator(
            inventory=inventory,
            triage=triage,
            reducer=reducer,
            merge=merge,
            fix=fix,
        )
        assert orch._inventory is inventory
        assert orch._triage is triage
        assert orch._reducer is reducer
        assert orch._merge is merge
        assert orch._fix is fix

    async def test_no_imports_from_omnibase_infra(self) -> None:
        """Handler must not import from omnibase_infra."""
        import importlib
        import inspect

        mod = importlib.import_module(
            "omnimarket.nodes.node_pr_lifecycle_orchestrator."
            "handlers.handler_pr_lifecycle_orchestrator"
        )
        source = inspect.getsource(mod)
        assert "from omnibase_infra" not in source
        assert "import omnibase_infra" not in source

    async def test_correlation_id_preserved_in_result(self) -> None:
        """correlation_id from command appears unchanged in result."""
        cid = uuid4()
        orch = _make_orchestrator()
        result = await orch.handle(_make_command(correlation_id=cid))
        assert result.correlation_id == cid

    async def test_only_merge_intents_when_no_fix_prs(self) -> None:
        """When reducer produces only MERGE intents, fix handler is never called."""
        inventory = MockInventory(prs=(_PR_GREEN,))
        triage = MockTriage(classified=(_TRIAGE_GREEN,))
        reducer = MockReducer(intents=(_INTENT_MERGE,))
        merge = MockMerge(prs_merged=1)
        fix = MockFix()

        orch = _make_orchestrator(
            inventory=inventory,
            triage=triage,
            reducer=reducer,
            merge=merge,
            fix=fix,
        )
        result = await orch.handle(_make_command())

        assert result.final_state == "COMPLETE"
        assert result.prs_merged == 1
        assert fix.call_count == 0

    async def test_parallel_fix_dispatch_n_prs(self) -> None:
        """N fix-intent PRs dispatch N parallel fix calls (one per PR)."""
        n = 5
        fix_triage = tuple(
            TriageRecord(
                pr_number=200 + i,
                repo="OmniNode-ai/omnimarket",
                category=EnumPrCategory.RED,
            )
            for i in range(n)
        )
        fix_intents = tuple(
            ReducerIntent(
                pr_number=200 + i,
                repo="OmniNode-ai/omnimarket",
                intent=EnumReducerIntent.FIX,
            )
            for i in range(n)
        )
        fix_prs_raw = tuple(
            PrRecord(
                pr_number=200 + i,
                repo="OmniNode-ai/omnimarket",
                checks_status="failure",
            )
            for i in range(n)
        )

        inventory = MockInventory(prs=fix_prs_raw)
        triage = MockTriage(classified=fix_triage)
        reducer = MockReducer(intents=fix_intents)
        fix = MockFix()  # prs_dispatched=None -> uses len(prs_to_fix)=1 per call

        orch = _make_orchestrator(
            inventory=inventory,
            triage=triage,
            reducer=reducer,
            fix=fix,
        )
        result = await orch.handle(_make_command(max_parallel_polish=n))

        assert result.final_state == "COMPLETE"
        # Each PR got its own fix call
        assert fix.call_count == n
        # Total dispatched = n (1 per call)
        assert result.prs_fixed == n
        # Every PR number was dispatched
        assert sorted(fix.dispatched_pr_numbers) == list(range(200, 200 + n))
        # With max_parallel_polish=n all tasks can run concurrently
        assert fix.max_in_flight > 1

    async def test_parallel_fix_respects_max_parallel_cap(self) -> None:
        """max_parallel_polish=1 serializes fix dispatches (call_count still == N)."""
        n = 3
        fix_triage = tuple(
            TriageRecord(
                pr_number=300 + i,
                repo="OmniNode-ai/omnimarket",
                category=EnumPrCategory.RED,
            )
            for i in range(n)
        )
        fix_intents = tuple(
            ReducerIntent(
                pr_number=300 + i,
                repo="OmniNode-ai/omnimarket",
                intent=EnumReducerIntent.FIX,
            )
            for i in range(n)
        )
        fix_prs_raw = tuple(
            PrRecord(
                pr_number=300 + i,
                repo="OmniNode-ai/omnimarket",
                checks_status="failure",
            )
            for i in range(n)
        )

        inventory = MockInventory(prs=fix_prs_raw)
        triage = MockTriage(classified=fix_triage)
        reducer = MockReducer(intents=fix_intents)
        fix = MockFix()

        orch = _make_orchestrator(
            inventory=inventory,
            triage=triage,
            reducer=reducer,
            fix=fix,
        )
        # max_parallel_polish=1 means fully serialized, but N calls still happen
        result = await orch.handle(_make_command(max_parallel_polish=1))

        assert result.final_state == "COMPLETE"
        assert fix.call_count == n
        assert result.prs_fixed == n
        # Semaphore cap of 1 means only 1 task in flight at a time
        assert fix.max_in_flight == 1

    async def test_max_parallel_polish_default_is_20(self) -> None:
        """ModelPrLifecycleStartCommand defaults max_parallel_polish to 20."""
        cmd = _make_command()
        assert cmd.max_parallel_polish == 20


@pytest.mark.unit
class TestPrLifecycleOrchestratorResultFile:
    """OMN-8391: orchestrator persists result.json for merge_sweep polling."""

    async def test_success_writes_result_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Successful sweep writes a ModelSkillResult-shaped result.json."""
        monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
        orch = _make_orchestrator(inventory=MockInventory(prs=()))
        cmd = _make_command(run_id="20260411-120000-abc123")

        result = await orch.handle(cmd)
        assert result.final_state == "COMPLETE"

        result_path = (
            tmp_path / "merge-sweep" / "20260411-120000-abc123" / "result.json"
        )
        assert result_path.exists(), f"result.json missing at {result_path}"

        payload = json.loads(result_path.read_text())
        assert payload["skill_name"] == "merge-sweep"
        assert payload["status"] == "success"
        assert payload["run_id"] == "20260411-120000-abc123"
        assert payload["final_state"] == "COMPLETE"
        assert payload["correlation_id"] == str(cmd.correlation_id)

    async def test_failure_writes_result_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Failed sweep still writes result.json so the skill can terminate."""
        monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))

        class ExplodingInventory:
            async def handle(
                self,
                *,
                correlation_id: UUID,
                repos: tuple[str, ...] = (),
                dry_run: bool = False,
            ) -> InventoryResult:
                raise RuntimeError("boom")

        orch = _make_orchestrator(inventory=ExplodingInventory())  # type: ignore[arg-type]
        cmd = _make_command(run_id="20260411-120001-fail99")

        result = await orch.handle(cmd)
        assert result.final_state == "FAILED"
        assert result.error_message == "boom"

        result_path = (
            tmp_path / "merge-sweep" / "20260411-120001-fail99" / "result.json"
        )
        assert result_path.exists()

        payload = json.loads(result_path.read_text())
        assert payload["status"] == "error"
        assert payload["final_state"] == "FAILED"
        assert payload["error_message"] == "boom"
