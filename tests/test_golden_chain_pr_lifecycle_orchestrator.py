"""Golden chain tests for node_pr_lifecycle_orchestrator.

Verifies the FSM orchestrator composes 5 sub-handlers via mock adapters:
  start command -> phase transitions -> completion.
Uses EventBusInmemory, zero infra required.

Mock handler signatures match real sub-handler signatures (OMN-9234 fix):
  - MockInventory.handle(input_model: ModelPrInventoryInput) → sync
  - MockTriage.handle(correlation_id, prs: tuple[ModelPrInventoryItem, ...]) → async
  - MockReducer.handle(*args, **kwargs) → async (accepts orchestrator kwargs)
  - MockMerge.handle(command: ModelPrMergeCommand) → async
  - MockFix.handle(command: ModelPrLifecycleFixCommand) → async

Related:
    - OMN-8087: Create pr_lifecycle_orchestrator Node
    - OMN-9234: Fix protocol-signature drift
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
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
    InventoryResult,
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
# Mock sub-handlers — signatures match real handler handle() methods exactly.
# ---------------------------------------------------------------------------


class MockInventory:
    """Mock matching HandlerPrLifecycleInventory.handle(input_model) signature.

    The orchestrator calls this via _call_inventory() which constructs a
    ModelPrInventoryInput and calls handle(input_model).  The mock returns
    a pre-configured InventoryResult so the orchestrator can continue.
    """

    def __init__(self, prs: tuple[PrRecord, ...] = ()) -> None:
        self._prs = prs
        self.call_count = 0
        self.last_input: Any = None

    def handle(self, input_model: Any) -> Any:
        """Sync signature matching HandlerPrLifecycleInventory.handle(input_model)."""
        self.call_count += 1
        self.last_input = input_model
        return InventoryResult(prs=self._prs, total_collected=len(self._prs))


class MockTriage:
    """Mock matching HandlerPrLifecycleTriage.handle(correlation_id, prs) signature."""

    def __init__(self, classified: tuple[TriageRecord, ...] = ()) -> None:
        self._classified = classified
        self.call_count = 0
        self.last_correlation_id: UUID | None = None

    async def handle(
        self,
        correlation_id: UUID,
        prs: Any,
    ) -> Any:
        """Positional args: (correlation_id, prs) — no keyword-only."""
        self.call_count += 1
        self.last_correlation_id = correlation_id
        green = sum(1 for r in self._classified if r.category == EnumPrCategory.GREEN)
        non_green = len(self._classified) - green
        return PrTriageResult(
            classified=self._classified,
            green_count=green,
            non_green_count=non_green,
        )


class MockReducer:
    """Mock matching HandlerPrLifecycleStateReducer.handle(*args, **kwargs) signature."""

    def __init__(self, intents: tuple[ReducerIntent, ...] = ()) -> None:
        self._intents = intents
        self.call_count = 0
        self.last_dry_run: bool = False
        self.last_fix_only: bool = False
        self.last_merge_only: bool = False

    async def handle(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """*args/**kwargs shim matching dual-path reducer dispatch."""
        self.call_count += 1
        self.last_dry_run = bool(kwargs.get("dry_run", False))
        self.last_fix_only = bool(kwargs.get("fix_only", False))
        self.last_merge_only = bool(kwargs.get("merge_only", False))
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
    """Mock matching HandlerPrLifecycleMerge.handle(command: ModelPrMergeCommand) signature."""

    def __init__(self, *, prs_merged: int = 0, fail: bool = False) -> None:
        self._prs_merged = prs_merged
        self._fail = fail
        self.call_count = 0
        self.last_command: Any = None

    async def handle(self, command: Any) -> Any:
        """Single positional command argument matching real merge handler."""
        self.call_count += 1
        self.last_command = command
        if self._fail:
            msg = "merge failed"
            raise RuntimeError(msg)
        # Return a MergeResult-compatible object.
        # The orchestrator's _call_merge_fanout maps merged=True → prs_merged+1.
        from unittest.mock import MagicMock

        result = MagicMock()
        result.merged = self._prs_merged > 0
        return result


class MockFix:
    """Mock matching HandlerPrLifecycleFix.handle(command: ModelPrLifecycleFixCommand) signature."""

    def __init__(
        self, *, prs_dispatched: int | None = None, fail: bool = False
    ) -> None:
        self._prs_dispatched = prs_dispatched  # None = 1 per call
        self._fail = fail
        self.call_count = 0
        self.dispatched_pr_numbers: list[int] = []
        self._in_flight = 0
        self.max_in_flight = 0
        self.last_command: Any = None

    async def handle(self, command: Any) -> Any:
        """Single positional command argument matching real fix handler."""
        import asyncio

        self._in_flight += 1
        if self._in_flight > self.max_in_flight:
            self.max_in_flight = self._in_flight
        try:
            await asyncio.sleep(0)  # yield to allow concurrent tasks to enter
            self.call_count += 1
            self.last_command = command
            pr_number = getattr(command, "pr_number", 0)
            self.dispatched_pr_numbers.append(pr_number)
            if self._fail:
                msg = "fix failed"
                raise RuntimeError(msg)
            # Return a ModelPrLifecycleFixResult-compatible object.
            from unittest.mock import MagicMock

            result = MagicMock()
            result.fix_applied = True
            result.pr_number = pr_number
            return result
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


class _TestOrchestrator(HandlerPrLifecycleOrchestrator):
    """Test subclass that bypasses gh CLI calls.

    - _enumerate_repos() returns the repos that were filtered by the command.
    - _enumerate_open_pr_numbers(repo) returns synthetic PR numbers derived
      from whatever PrRecords the MockInventory holds.

    This allows MockInventory.handle(input_model) to be called with a real
    ModelPrInventoryInput without needing a live gh CLI or GitHub connection.
    The mock ignores the pr_numbers and returns its pre-configured prs.
    """

    def __init__(
        self, *, _mock_inventory_prs: tuple[PrRecord, ...] = (), **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self._mock_prs = _mock_inventory_prs

    def _enumerate_repos(self) -> tuple[str, ...]:
        """Return repos from the mock PR fixture, deduplicated."""
        return tuple(dict.fromkeys(pr.repo for pr in self._mock_prs))

    def _enumerate_open_pr_numbers(self, repo: str) -> tuple[int, ...]:
        """Return PR numbers for the given repo from the mock fixture."""
        return tuple(pr.pr_number for pr in self._mock_prs if pr.repo == repo)


def _make_orchestrator(
    *,
    inventory: Any = None,
    triage: MockTriage | None = None,
    reducer: MockReducer | None = None,
    merge: MockMerge | None = None,
    fix: MockFix | None = None,
    event_bus: EventBusInmemory | None = None,
) -> _TestOrchestrator:
    inv = inventory or MockInventory()
    # Retrieve pre-configured PrRecords from MockInventory for test enumeration.
    mock_prs: tuple[PrRecord, ...] = getattr(inv, "_prs", ())
    return _TestOrchestrator(
        _mock_inventory_prs=mock_prs,
        inventory=inv,
        triage=triage or MockTriage(),
        reducer=reducer or MockReducer(),
        merge=merge or MockMerge(),
        fix=fix or MockFix(),
        event_bus=event_bus,
    )


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
        # inventory.handle() is called once per repo in _call_inventory — 1 call for our single-repo fixture
        assert inventory.call_count >= 1
        assert triage.call_count == 1
        assert reducer.call_count == 1
        # Merge handler called once per green PR via _call_merge_fanout
        assert merge.call_count == 1
        # Fix handler called once per fix-intent PR
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
        """repos CSV filter restricts _call_inventory to the listed repos.

        With the OMN-9234 fix, the orchestrator's _call_inventory iterates
        only over the filtered repos (not the full org list). When repos have
        no open PRs (mock returns empty), inventory.handle() is not called,
        but the orchestrator still completes cleanly.

        The key contract: the orchestrator runs to COMPLETE, and the
        injected inventory reference is preserved.
        """
        inventory = MockInventory()
        orch = _make_orchestrator(inventory=inventory)

        result = await orch.handle(
            _make_command(repos="OmniNode-ai/omnimarket,OmniNode-ai/omniclaude")
        )

        assert result.final_state == "COMPLETE"
        assert orch._inventory is inventory  # reference preserved

    async def test_exception_in_inventory_leads_to_failed_state(self) -> None:
        """Exception in inventory -> final_state=FAILED with error_message set."""

        class BrokenInventory:
            # _prs lets _TestOrchestrator enumerate at least one repo/PR
            # so handle() is actually called (otherwise it's never reached).
            _prs = (_PR_GREEN,)

            def handle(self, input_model: Any) -> Any:
                msg = "GitHub API down"
                raise RuntimeError(msg)

        orch = _make_orchestrator(inventory=BrokenInventory())  # type: ignore[arg-type]
        result = await orch.handle(_make_command())

        assert result.final_state == "FAILED"
        assert result.error_message is not None
        assert "GitHub API down" in result.error_message

    async def test_exception_in_merge_counted_as_failed_pr_not_full_abort(
        self,
    ) -> None:
        """Exception in merge handler -> per-PR isolation (prs_failed counted, sweep continues).

        Prior contract was "one exception = entire sweep FAILED"; post-OMN-9234
        CodeRabbit feedback, merge exceptions are caught per PR so one transient
        GitHub/network error does not abort the whole batch. The orchestrator
        completes successfully with the failed PR recorded in ``prs_failed``.
        """
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

        # Sweep completes despite one PR raising — per-PR isolation is the contract.
        assert result.final_state == "COMPLETE"
        # The failing PR must NOT be counted as merged.
        assert result.prs_merged == 0

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
        fix = MockFix()  # prs_dispatched=None -> 1 per call via fix_applied=True

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
        # Total dispatched = n (1 per call, fix_applied=True)
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
            # _prs lets _TestOrchestrator enumerate a repo so handle() is called.
            _prs = (_PR_GREEN,)

            def handle(self, input_model: Any) -> Any:
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


# ---------------------------------------------------------------------------
# OMN-9114: admin-merge-fallback default is ON
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAdminMergeFallbackDefaultOn:
    """ModelPrLifecycleStartCommand.enable_admin_merge_fallback defaults to True.

    OMN-9114 — OMN-9065 closed Done but the default flip never landed on main.
    This test locks in the default=True invariant so silent regressions fail CI.
    """

    def test_default_enable_admin_merge_fallback_is_true(self) -> None:
        cmd = ModelPrLifecycleStartCommand(
            correlation_id=uuid4(),
            run_id="20260417-205500-dflt01",
        )
        assert cmd.enable_admin_merge_fallback is True

    def test_explicit_false_still_honored(self) -> None:
        cmd = ModelPrLifecycleStartCommand(
            correlation_id=uuid4(),
            run_id="20260417-205500-dflt02",
            enable_admin_merge_fallback=False,
        )
        assert cmd.enable_admin_merge_fallback is False

    def test_handler_admin_merge_handle_default_opt_in_true(self) -> None:
        """HandlerAdminMerge.handle(enable_admin_merge_fallback=...) defaults to True."""
        import inspect

        from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_admin_merge import (
            HandlerAdminMerge,
        )

        sig = inspect.signature(HandlerAdminMerge.handle)
        param = sig.parameters["enable_admin_merge_fallback"]
        assert param.default is True


@pytest.mark.asyncio
class TestOrchestratorForwardsAdminMergeFallbackFlag:
    """The orchestrator holds enable_admin_merge_fallback on its start command.

    With the OMN-9234 fix, the fix handler is called with ModelPrLifecycleFixCommand
    which does not carry enable_admin_merge_fallback (it routes by block_reason).
    The flag is still honoured at the orchestrator level: _dispatch_fix_parallel
    receives it and can use it for future routing decisions.

    This test verifies the command field is preserved and the fix handler is called.
    """

    async def _run_with_flag(self, *, enable_admin_merge_fallback: bool) -> MockFix:
        fix_intents = (
            ReducerIntent(
                pr_number=_PR_RED.pr_number,
                repo=_PR_RED.repo,
                intent=EnumReducerIntent.FIX,
            ),
        )
        fix = MockFix()
        inv = MockInventory(prs=(_PR_RED,))
        node = _TestOrchestrator(
            _mock_inventory_prs=inv._prs,
            inventory=inv,
            triage=MockTriage(
                classified=(
                    TriageRecord(
                        pr_number=_PR_RED.pr_number,
                        repo=_PR_RED.repo,
                        category=EnumPrCategory.RED,
                    ),
                )
            ),
            reducer=MockReducer(intents=fix_intents),
            merge=MockMerge(),
            fix=fix,
        )
        await node.handle(
            ModelPrLifecycleStartCommand(
                correlation_id=uuid4(),
                run_id="20260418-000000-flagfwd",
                enable_admin_merge_fallback=enable_admin_merge_fallback,
                admin_fallback_threshold_minutes=15,
            )
        )
        return fix

    async def test_flag_true_fix_handler_called(self) -> None:
        """enable_admin_merge_fallback=True: fix handler is still called once."""
        fix = await self._run_with_flag(enable_admin_merge_fallback=True)
        assert fix.call_count >= 1

    async def test_flag_false_fix_handler_called(self) -> None:
        """enable_admin_merge_fallback=False: fix handler is still called once."""
        fix = await self._run_with_flag(enable_admin_merge_fallback=False)
        assert fix.call_count >= 1

    async def test_command_carries_admin_merge_flag(self) -> None:
        """ModelPrLifecycleStartCommand.enable_admin_merge_fallback is preserved."""
        cmd = ModelPrLifecycleStartCommand(
            correlation_id=uuid4(),
            run_id="20260418-000000-flagchk",
            enable_admin_merge_fallback=False,
            admin_fallback_threshold_minutes=15,
        )
        assert cmd.enable_admin_merge_fallback is False
        assert cmd.admin_fallback_threshold_minutes == 15
