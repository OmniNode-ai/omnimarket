# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full declared-state ORCHESTRATOR coverage for node_pr_lifecycle_orchestrator,
driven over the canonical in-memory bus.

OMN-13674 (cluster pr_lifecycle_pipeline, archetype orchestrator). The orchestrator
is a declarative FSM composing five injected sub-handlers. This module drives the
contract-declared FSM + handler_routing end to end over ``EventBusInmemory`` (via
the ``integration_event_bus`` fixture) and asserts, over the bus:

  * every declared FSM ``state`` is entered on some path;
  * every declared ``transition`` edge fires — including every ``* -> FAILED``
    failure->terminal-error edge;
  * every sub-handler *route* (inventory / triage / reducer / merge / fix) fires
    on the path that reaches it;
  * the correct terminal event lands on the declared completed topic with the
    correct ``final_state`` per path (COMPLETE / FAILED / NOT_DONE), asserting
    typed result fields — never "returned without raising".

How it is wired
---------------
The orchestrator is dispatched through ``LocalRuntimeBusAdapter`` over the
in-memory bus: a ``ModelPrLifecycleStartCommand`` lands on the declared command
topic ``onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1`` and the terminal
``ModelPrLifecycleResult`` is auto-published onto the declared completed topic.
The handler itself publishes an explicit phase-transition event on
``onex.evt.omnimarket.pr-lifecycle-orchestrator-phase-transition.v1`` for *every*
FSM transition, so the observed edge set is read directly off that topic — the
FSM is asserted through the bus, not through internal state.

The five sub-handlers and the gh-CLI seams are replaced by constructor-injected
mocks / method overrides (``_Mock*`` + ``_HarnessOrchestrator``). NO subprocess is
run and NO real Kafka is touched. The reducer intent set the mock returns is what
routes PRs to MERGING vs FIXING vs SKIP, so a single harness drives every path.

Failure edges. Three ``* -> FAILED`` edges are reachable by a sub-handler raise at
a non-isolated call site (INVENTORYING via inventory, TRIAGING via reducer, FIXING
via the fix ExceptionGroup). The remaining three (MERGING, VERIFYING,
POST_MERGE_TAIL) are only reachable by injecting a fault into the single
un-isolated operation inside that phase window, because the handler isolates
sub-handler faults per-PR in those phases (``_call_merge_fanout`` and
``_run_verification`` both catch per-item). Those faults are injected at the real
phase-boundary seams (``_call_merge_fanout`` / ``_run_verification`` /
``_record_ledger_event`` on the POST_MERGE_TAIL conclusion write) so the genuine
FSM ``except -> _transition_phase(FAILED)`` machinery is exercised from each state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_pr_arm_gate_compute.models.model_arm_gate_policy import (
    EnumArmActionMode,
)
from omnimarket.nodes.node_pr_lifecycle_inventory_compute.models.model_pr_lifecycle_inventory import (
    ModelOrgWideOpenPrInventory,
    ModelOrgWideOpenPrRemainder,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    TOPIC_COMPLETED,
    TOPIC_PHASE_TRANSITION,
    TOPIC_PR_LIFECYCLE_START,
    EnumOrchestratorState,
    HandlerPrLifecycleOrchestrator,
    ModelPrLifecycleResult,
    ModelPrLifecycleStartCommand,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.occ_stamp_readback import (
    ModelOccStampReadbackResult,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
    EnumPrCategory,
    EnumReducerIntent,
    FixResult,
    InventoryResult,
    PrRecord,
    PrTriageResult,
    ReducerIntent,
    ReducerResult,
    TriageRecord,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.verify_target_mapping import (
    EnumVerificationOutcome,
)
from omnimarket.nodes.pr_ledger_native import EnumOrchestratorAction
from tests.runtime_local_compat import LocalRuntimeBusAdapter

_REPO = "OmniNode-ai/omnimarket"

# ---------------------------------------------------------------------------
# The full contract-declared FSM edge set (node_pr_lifecycle_orchestrator/
# contract.yaml -> fsm.transitions), as lowercased (from_phase, to_phase) tuples
# — the exact form the handler publishes on the phase-transition topic.
# ---------------------------------------------------------------------------
_DECLARED_EDGES: frozenset[tuple[str, str]] = frozenset(
    {
        ("idle", "inventorying"),
        ("inventorying", "triaging"),
        ("inventorying", "complete"),
        ("inventorying", "failed"),
        ("triaging", "merging"),
        ("triaging", "fixing"),
        ("triaging", "complete"),
        ("triaging", "verifying"),
        ("verifying", "merging"),
        ("verifying", "failed"),
        ("merging", "post_merge_tail"),
        ("post_merge_tail", "fixing"),
        ("post_merge_tail", "complete"),
        ("post_merge_tail", "failed"),
        ("fixing", "complete"),
        ("triaging", "failed"),
        ("merging", "failed"),
        ("fixing", "failed"),
    }
)

_DECLARED_STATES: frozenset[str] = frozenset(
    s.value.lower() for s in EnumOrchestratorState
)


# ---------------------------------------------------------------------------
# Mock sub-handlers. Each records that its route fired and returns an
# orchestrator-internal aggregate directly (the documented handler short-circuit
# for inventory / triage / fix; the reducer + merge are consumed directly).
# ---------------------------------------------------------------------------


@dataclass
class _Calls:
    inventory: int = 0
    triage: int = 0
    reducer: int = 0
    merge: int = 0
    fix: int = 0


class _MockInventory:
    """Sync ``handle(input_model)`` returning an ``InventoryResult`` directly."""

    def __init__(self, calls: _Calls, result: InventoryResult, raises: bool) -> None:
        self._calls = calls
        self._result = result
        self._raises = raises

    def handle(self, input_model: Any) -> InventoryResult:
        self._calls.inventory += 1
        if self._raises:
            raise RuntimeError("inventory boom")
        return self._result


class _MockTriage:
    def __init__(self, calls: _Calls, result: PrTriageResult, raises: bool) -> None:
        self._calls = calls
        self._result = result
        self._raises = raises

    async def handle(
        self, correlation_id: UUID, prs: tuple[Any, ...]
    ) -> PrTriageResult:
        self._calls.triage += 1
        if self._raises:
            raise RuntimeError("triage boom")
        return self._result


class _MockReducer:
    def __init__(self, calls: _Calls, result: ReducerResult, raises: bool) -> None:
        self._calls = calls
        self._result = result
        self._raises = raises

    async def handle(self, *args: Any, **kwargs: Any) -> ReducerResult:
        self._calls.reducer += 1
        if self._raises:
            raise RuntimeError("reducer boom")
        return self._result


@dataclass
class _MergeReply:
    merged: bool


class _MockMerge:
    def __init__(self, calls: _Calls, merged: bool) -> None:
        self._calls = calls
        self._merged = merged

    async def handle(self, command: Any) -> _MergeReply:
        self._calls.merge += 1
        return _MergeReply(merged=self._merged)


class _MockFix:
    def __init__(self, calls: _Calls, raises: bool) -> None:
        self._calls = calls
        self._raises = raises

    async def handle(self, command: Any) -> FixResult:
        self._calls.fix += 1
        if self._raises:
            raise RuntimeError("fix boom")
        return FixResult(prs_dispatched=1, prs_skipped=0)


class _VerifiedOccStampReadback:
    """Hermetic OCC-companion read-back stub (OMN-14151 arm-gate).

    Always verifies so the arm-gate's occ_companion_verified criterion is
    satisfied without any live gh/remote call.
    """

    async def verify_fix_landed(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> ModelOccStampReadbackResult:
        return ModelOccStampReadbackResult(verified=True, reason="test: stubbed")


class _HarnessOrchestrator(HandlerPrLifecycleOrchestrator):
    """Orchestrator with the gh-CLI seams replaced by injected fixtures.

    Every FSM / routing / ledger code path runs unchanged; only the network
    seams (repo + PR enumeration, org-wide census, changed-files, verification
    probe) and — where a declared failure edge has no naturally-injectable seam
    — the phase-boundary wrapper are overridden.
    """

    def __init__(
        self,
        *,
        census: ModelOrgWideOpenPrInventory | None,
        probe_outcome: EnumVerificationOutcome | None,
        merge_fanout_raises: bool = False,
        run_verification_raises: bool = False,
        fault_on_action: EnumOrchestratorAction | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("occ_stamp_readback", _VerifiedOccStampReadback())
        super().__init__(**kwargs)
        self._census = census
        self._probe_outcome = probe_outcome
        self._merge_fanout_raises = merge_fanout_raises
        self._run_verification_raises = run_verification_raises
        self._fault_on_action = fault_on_action

    def _enumerate_repos(self) -> tuple[str, ...]:
        return (_REPO,)

    def _enumerate_open_pr_numbers(self, repo: str) -> tuple[int, ...]:
        return (1,)

    def _collect_org_wide_open_prs(self) -> Any:
        return self._census

    def _pr_changed_files(self, repo: str, pr_number: int) -> list[str]:
        # A non-empty code-file list -> a code verification target (never
        # SKIPPED_NO_MAPPING), so the verification probe is actually invoked.
        return ["src/omnimarket/nodes/node_x/handlers/handler_x.py"]

    async def _execute_verification_probe(
        self, *, target: Any, timeout_seconds: int
    ) -> EnumVerificationOutcome:
        assert self._probe_outcome is not None
        return self._probe_outcome

    async def _call_merge_fanout(self, **kwargs: Any) -> Any:
        if self._merge_fanout_raises:
            raise RuntimeError("merge fanout boom")
        return await super()._call_merge_fanout(**kwargs)

    async def _run_verification(self, **kwargs: Any) -> Any:
        if self._run_verification_raises:
            raise RuntimeError("verification boom")
        return await super()._run_verification(**kwargs)

    def _record_ledger_event(self, **kwargs: Any) -> None:
        # Fault-inject the single un-isolated ledger write of a phase window so
        # the declared POST_MERGE_TAIL -> FAILED edge is exercised (the MERGED
        # conclusion is written while state.fsm == POST_MERGE_TAIL).
        if (
            self._fault_on_action is not None
            and kwargs.get("orchestrator_action") == self._fault_on_action
        ):
            raise RuntimeError("ledger fault injection")
        super()._record_ledger_event(**kwargs)


# ---------------------------------------------------------------------------
# Fixture builders.
# ---------------------------------------------------------------------------


def _inventory(pr_numbers: tuple[int, ...]) -> InventoryResult:
    return InventoryResult(
        prs=tuple(
            PrRecord(
                pr_number=n,
                repo=_REPO,
                title=f"pr {n}",
                # OMN-14151: arm-gate-ready facts so scenarios asserting a real
                # merge continue to pass under the merge-queue governor's
                # fail-closed arm gate. Harmless for non-merge paths.
                checks_status="success",
                is_draft=False,
                coderabbit_unresolved=0,
                merge_state_status="CLEAN",
            )
            for n in pr_numbers
        ),
        total_collected=len(pr_numbers),
    )


def _triage(pr_numbers: tuple[int, ...], category: EnumPrCategory) -> PrTriageResult:
    classified = tuple(
        TriageRecord(pr_number=n, repo=_REPO, category=category) for n in pr_numbers
    )
    green = sum(1 for r in classified if r.category == EnumPrCategory.GREEN)
    return PrTriageResult(
        classified=classified,
        green_count=green,
        non_green_count=len(classified) - green,
    )


def _reducer(intents: dict[int, EnumReducerIntent]) -> ReducerResult:
    intent_tuple = tuple(
        ReducerIntent(pr_number=n, repo=_REPO, intent=i) for n, i in intents.items()
    )
    return ReducerResult(
        intents=intent_tuple,
        merge_count=sum(1 for i in intent_tuple if i.intent == EnumReducerIntent.MERGE),
        fix_count=sum(1 for i in intent_tuple if i.intent == EnumReducerIntent.FIX),
        skip_count=sum(1 for i in intent_tuple if i.intent == EnumReducerIntent.SKIP),
    )


def _clean_census() -> ModelOrgWideOpenPrInventory:
    """Zero org-wide open PRs -> the done-gate is satisfied (COMPLETE stands)."""
    return ModelOrgWideOpenPrInventory(open_count=0)


@dataclass
class _Scenario:
    """One driven sweep: the resulting bus terminal event + observed FSM edges."""

    result: ModelPrLifecycleResult
    edges: tuple[tuple[str, str], ...]
    calls: _Calls
    states_entered: frozenset[str] = field(default_factory=frozenset)


async def _drive(
    bus: Any,
    *,
    command: ModelPrLifecycleStartCommand,
    inventory: _MockInventory,
    triage: _MockTriage,
    reducer: _MockReducer,
    merge: _MockMerge,
    fix: _MockFix,
    census: ModelOrgWideOpenPrInventory | None,
    probe_outcome: EnumVerificationOutcome | None = None,
    merge_fanout_raises: bool = False,
    run_verification_raises: bool = False,
    fault_on_action: EnumOrchestratorAction | None = None,
) -> _Scenario:
    orch = _HarnessOrchestrator(
        inventory=inventory,
        triage=triage,
        reducer=reducer,
        merge=merge,
        fix=fix,
        event_bus=bus,
        census=census,
        probe_outcome=probe_outcome,
        merge_fanout_raises=merge_fanout_raises,
        run_verification_raises=run_verification_raises,
        fault_on_action=fault_on_action,
    )
    adapter = LocalRuntimeBusAdapter(
        handler=orch,
        handler_name="pr-lifecycle-orchestrator",
        input_model_cls=ModelPrLifecycleStartCommand,
        output_topic=TOPIC_COMPLETED,
        bus=bus,
    )
    await bus.subscribe(
        TOPIC_PR_LIFECYCLE_START,
        on_message=adapter.on_message,
        group_id="omnimarket-pr-lifecycle-orchestrator-test",
    )
    await bus.publish(
        TOPIC_PR_LIFECYCLE_START,
        key=None,
        value=command.model_dump_json().encode("utf-8"),
    )

    completed = await bus.get_event_history(topic=TOPIC_COMPLETED)
    assert len(completed) == 1, f"expected exactly one terminal event, got {completed}"
    result = ModelPrLifecycleResult.model_validate(json.loads(completed[-1].value))

    transitions = await bus.get_event_history(topic=TOPIC_PHASE_TRANSITION)
    edges: list[tuple[str, str]] = []
    for event in transitions:
        payload = json.loads(event.value)
        edges.append((payload["from_phase"], payload["to_phase"]))
    states = {e[0] for e in edges} | {e[1] for e in edges}
    return _Scenario(
        result=result,
        edges=tuple(edges),
        calls=inventory._calls,
        states_entered=frozenset(states),
    )


def _command(run_id: str, **flags: Any) -> ModelPrLifecycleStartCommand:
    defaults: dict[str, Any] = {
        # OMN-14151: this suite proves FSM/routing/ledger wiring, not the
        # arm-gate's report-only default (covered separately) — opt into
        # ENFORCE by default so existing merge-path assertions keep passing.
        "action_mode": EnumArmActionMode.ENFORCE,
        "merge_queue_mutation_kill_switch": False,
    }
    defaults.update(flags)
    return ModelPrLifecycleStartCommand(
        correlation_id=uuid4(), run_id=run_id, repos=_REPO, **defaults
    )


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep result.json writes inside the test's tmp dir, never ``~/.onex_state``."""
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))


# ===========================================================================
# Per-path FSM + routing + terminal-event assertions (COMPLETE paths).
# ===========================================================================


@pytest.mark.integration
async def test_merge_path_drives_idle_to_complete_over_bus(
    integration_event_bus: Any,
) -> None:
    """Green PRs with MERGE intent: IDLE->INVENTORYING->TRIAGING->MERGING->
    POST_MERGE_TAIL->COMPLETE. inventory/triage/reducer/merge routes all fire."""
    bus = integration_event_bus
    await bus.start()
    try:
        calls = _Calls()
        scenario = await _drive(
            bus,
            command=_command("run-merge"),
            inventory=_MockInventory(calls, _inventory((1, 2)), raises=False),
            triage=_MockTriage(
                calls, _triage((1, 2), EnumPrCategory.GREEN), raises=False
            ),
            reducer=_MockReducer(
                calls,
                _reducer({1: EnumReducerIntent.MERGE, 2: EnumReducerIntent.MERGE}),
                raises=False,
            ),
            merge=_MockMerge(calls, merged=True),
            fix=_MockFix(calls, raises=False),
            census=_clean_census(),
        )
        assert scenario.edges == (
            ("idle", "inventorying"),
            ("inventorying", "triaging"),
            ("triaging", "merging"),
            ("merging", "post_merge_tail"),
            ("post_merge_tail", "complete"),
        )
        assert scenario.result.final_state == EnumOrchestratorState.COMPLETE.value
        assert scenario.result.prs_inventoried == 2
        assert scenario.result.prs_merged == 2
        assert scenario.result.prs_fixed == 0
        # Routes: merge fired, fix did not (no FIX intents).
        assert calls.inventory == 1
        assert calls.triage == 1
        assert calls.reducer == 1
        assert calls.merge == 2
        assert calls.fix == 0
    finally:
        await bus.close()


@pytest.mark.integration
async def test_merge_then_fix_path_reaches_fixing_over_bus(
    integration_event_bus: Any,
) -> None:
    """Mixed MERGE + FIX intents: POST_MERGE_TAIL->FIXING->COMPLETE. Both the
    merge and fix routes fire; the terminal event reports both counts."""
    bus = integration_event_bus
    await bus.start()
    try:
        calls = _Calls()
        scenario = await _drive(
            bus,
            command=_command("run-merge-fix"),
            inventory=_MockInventory(calls, _inventory((1, 2)), raises=False),
            triage=_MockTriage(
                calls, _triage((1, 2), EnumPrCategory.RED), raises=False
            ),
            reducer=_MockReducer(
                calls,
                _reducer({1: EnumReducerIntent.MERGE, 2: EnumReducerIntent.FIX}),
                raises=False,
            ),
            merge=_MockMerge(calls, merged=True),
            fix=_MockFix(calls, raises=False),
            census=_clean_census(),
        )
        assert scenario.edges == (
            ("idle", "inventorying"),
            ("inventorying", "triaging"),
            ("triaging", "merging"),
            ("merging", "post_merge_tail"),
            ("post_merge_tail", "fixing"),
            ("fixing", "complete"),
        )
        assert scenario.result.final_state == EnumOrchestratorState.COMPLETE.value
        assert scenario.result.prs_merged == 1
        assert scenario.result.prs_fixed == 1
        assert calls.merge == 1
        assert calls.fix == 1
    finally:
        await bus.close()


@pytest.mark.integration
async def test_fix_only_path_triaging_to_fixing_over_bus(
    integration_event_bus: Any,
) -> None:
    """fix_only: TRIAGING->FIXING->COMPLETE (MERGING is skipped entirely)."""
    bus = integration_event_bus
    await bus.start()
    try:
        calls = _Calls()
        scenario = await _drive(
            bus,
            command=_command("run-fixonly", fix_only=True),
            inventory=_MockInventory(calls, _inventory((1,)), raises=False),
            triage=_MockTriage(calls, _triage((1,), EnumPrCategory.RED), raises=False),
            reducer=_MockReducer(
                calls, _reducer({1: EnumReducerIntent.FIX}), raises=False
            ),
            merge=_MockMerge(calls, merged=True),
            fix=_MockFix(calls, raises=False),
            census=_clean_census(),
        )
        assert scenario.edges == (
            ("idle", "inventorying"),
            ("inventorying", "triaging"),
            ("triaging", "fixing"),
            ("fixing", "complete"),
        )
        assert scenario.result.final_state == EnumOrchestratorState.COMPLETE.value
        assert scenario.result.prs_fixed == 1
        assert calls.fix == 1
        assert calls.merge == 0
    finally:
        await bus.close()


@pytest.mark.integration
async def test_inventory_only_path_inventorying_to_complete_over_bus(
    integration_event_bus: Any,
) -> None:
    """inventory_only: IDLE->INVENTORYING->COMPLETE (no triage/reducer/merge/fix)."""
    bus = integration_event_bus
    await bus.start()
    try:
        calls = _Calls()
        scenario = await _drive(
            bus,
            command=_command("run-invonly", inventory_only=True),
            inventory=_MockInventory(calls, _inventory((1, 2, 3)), raises=False),
            triage=_MockTriage(calls, _triage((), EnumPrCategory.GREEN), raises=False),
            reducer=_MockReducer(calls, _reducer({}), raises=False),
            merge=_MockMerge(calls, merged=True),
            fix=_MockFix(calls, raises=False),
            census=_clean_census(),
        )
        assert scenario.edges == (
            ("idle", "inventorying"),
            ("inventorying", "complete"),
        )
        assert scenario.result.final_state == EnumOrchestratorState.COMPLETE.value
        assert scenario.result.prs_inventoried == 3
        # Downstream routes never fired.
        assert calls.triage == 0
        assert calls.reducer == 0
        assert calls.merge == 0
        assert calls.fix == 0
    finally:
        await bus.close()


@pytest.mark.integration
async def test_dry_run_path_triaging_to_complete_over_bus(
    integration_event_bus: Any,
) -> None:
    """dry_run: TRIAGING->COMPLETE. Intents are recorded (prs_skipped) but the
    merge/fix effect routes never fire."""
    bus = integration_event_bus
    await bus.start()
    try:
        calls = _Calls()
        scenario = await _drive(
            bus,
            command=_command("run-dry", dry_run=True),
            inventory=_MockInventory(calls, _inventory((1, 2)), raises=False),
            triage=_MockTriage(
                calls, _triage((1, 2), EnumPrCategory.GREEN), raises=False
            ),
            reducer=_MockReducer(
                calls,
                _reducer({1: EnumReducerIntent.MERGE, 2: EnumReducerIntent.MERGE}),
                raises=False,
            ),
            merge=_MockMerge(calls, merged=True),
            fix=_MockFix(calls, raises=False),
            census=_clean_census(),
        )
        assert scenario.edges == (
            ("idle", "inventorying"),
            ("inventorying", "triaging"),
            ("triaging", "complete"),
        )
        assert scenario.result.final_state == EnumOrchestratorState.COMPLETE.value
        assert scenario.result.prs_skipped == 2
        assert scenario.result.prs_merged == 0
        assert calls.reducer == 1
        assert calls.merge == 0
        assert calls.fix == 0
    finally:
        await bus.close()


@pytest.mark.integration
async def test_skip_only_path_triaging_to_complete_over_bus(
    integration_event_bus: Any,
) -> None:
    """All SKIP intents (no actionable PRs), not dry_run: TRIAGING->COMPLETE with
    no MERGING/FIXING. The SKIP route is folded into prs_skipped."""
    bus = integration_event_bus
    await bus.start()
    try:
        calls = _Calls()
        scenario = await _drive(
            bus,
            command=_command("run-skip"),
            inventory=_MockInventory(calls, _inventory((1, 2)), raises=False),
            triage=_MockTriage(
                calls, _triage((1, 2), EnumPrCategory.NEEDS_REVIEW), raises=False
            ),
            reducer=_MockReducer(
                calls,
                _reducer({1: EnumReducerIntent.SKIP, 2: EnumReducerIntent.SKIP}),
                raises=False,
            ),
            merge=_MockMerge(calls, merged=True),
            fix=_MockFix(calls, raises=False),
            census=_clean_census(),
        )
        assert scenario.edges == (
            ("idle", "inventorying"),
            ("inventorying", "triaging"),
            ("triaging", "complete"),
        )
        assert scenario.result.final_state == EnumOrchestratorState.COMPLETE.value
        assert scenario.result.prs_skipped == 2
        assert scenario.result.prs_merged == 0
        assert scenario.result.prs_fixed == 0
        assert calls.merge == 0
        assert calls.fix == 0
    finally:
        await bus.close()


@pytest.mark.integration
async def test_merge_only_path_post_merge_tail_to_complete_over_bus(
    integration_event_bus: Any,
) -> None:
    """merge_only with a FIX intent present: MERGING->POST_MERGE_TAIL->COMPLETE.
    The FIXING phase is skipped even though a FIX intent exists."""
    bus = integration_event_bus
    await bus.start()
    try:
        calls = _Calls()
        scenario = await _drive(
            bus,
            command=_command("run-mergeonly", merge_only=True),
            inventory=_MockInventory(calls, _inventory((1, 2)), raises=False),
            triage=_MockTriage(
                calls, _triage((1, 2), EnumPrCategory.GREEN), raises=False
            ),
            reducer=_MockReducer(
                calls,
                _reducer({1: EnumReducerIntent.MERGE, 2: EnumReducerIntent.FIX}),
                raises=False,
            ),
            merge=_MockMerge(calls, merged=True),
            fix=_MockFix(calls, raises=False),
            census=_clean_census(),
        )
        assert scenario.edges == (
            ("idle", "inventorying"),
            ("inventorying", "triaging"),
            ("triaging", "merging"),
            ("merging", "post_merge_tail"),
            ("post_merge_tail", "complete"),
        )
        assert scenario.result.final_state == EnumOrchestratorState.COMPLETE.value
        assert scenario.result.prs_merged == 1
        # merge_only -> the FIX intent is never dispatched.
        assert calls.merge == 1
        assert calls.fix == 0
    finally:
        await bus.close()


# ===========================================================================
# VERIFYING phase (verify=True): TRIAGING->VERIFYING->MERGING + the negative
# control that a VERIFICATION_FAILED PR is blocked (stays open).
# ===========================================================================


@pytest.mark.integration
async def test_verify_pass_path_triaging_verifying_merging_over_bus(
    integration_event_bus: Any,
) -> None:
    """verify=True, probe MERGED: TRIAGING->VERIFYING->MERGING->POST_MERGE_TAIL->
    COMPLETE. The verified PR is counted and merged."""
    bus = integration_event_bus
    await bus.start()
    try:
        calls = _Calls()
        scenario = await _drive(
            bus,
            command=_command("run-verify-pass", verify=True),
            inventory=_MockInventory(calls, _inventory((1,)), raises=False),
            triage=_MockTriage(
                calls, _triage((1,), EnumPrCategory.GREEN), raises=False
            ),
            reducer=_MockReducer(
                calls, _reducer({1: EnumReducerIntent.MERGE}), raises=False
            ),
            merge=_MockMerge(calls, merged=True),
            fix=_MockFix(calls, raises=False),
            census=_clean_census(),
            probe_outcome=EnumVerificationOutcome.MERGED,
        )
        assert scenario.edges == (
            ("idle", "inventorying"),
            ("inventorying", "triaging"),
            ("triaging", "verifying"),
            ("verifying", "merging"),
            ("merging", "post_merge_tail"),
            ("post_merge_tail", "complete"),
        )
        assert scenario.result.final_state == EnumOrchestratorState.COMPLETE.value
        assert scenario.result.prs_verified == 1
        assert scenario.result.prs_verification_blocked == 0
        assert scenario.result.prs_merged == 1
        assert calls.merge == 1
    finally:
        await bus.close()


@pytest.mark.integration
async def test_verify_failed_blocks_pr_negative_control_over_bus(
    integration_event_bus: Any,
) -> None:
    """Negative control: a VERIFICATION_FAILED PR MUST be blocked — it is left
    open (prs_verification_blocked=1) and never merged, even though its reducer
    intent was MERGE. Only VERIFICATION_FAILED blocks; the FSM still completes."""
    bus = integration_event_bus
    await bus.start()
    try:
        calls = _Calls()
        scenario = await _drive(
            bus,
            command=_command("run-verify-fail", verify=True),
            inventory=_MockInventory(calls, _inventory((1,)), raises=False),
            triage=_MockTriage(
                calls, _triage((1,), EnumPrCategory.GREEN), raises=False
            ),
            reducer=_MockReducer(
                calls, _reducer({1: EnumReducerIntent.MERGE}), raises=False
            ),
            merge=_MockMerge(calls, merged=True),
            fix=_MockFix(calls, raises=False),
            census=_clean_census(),
            probe_outcome=EnumVerificationOutcome.VERIFICATION_FAILED,
        )
        # VERIFYING entered, but with zero cleared PRs there is no MERGING edge.
        assert ("triaging", "verifying") in scenario.edges
        assert ("verifying", "merging") not in scenario.edges
        assert scenario.result.final_state == EnumOrchestratorState.COMPLETE.value
        assert scenario.result.prs_verification_blocked == 1
        assert scenario.result.prs_merged == 0
        # The failed PR must never reach the merge route.
        assert calls.merge == 0
        assert (
            scenario.result.verification_breakdown[
                EnumVerificationOutcome.VERIFICATION_FAILED.value
            ]
            == 1
        )
    finally:
        await bus.close()


# ===========================================================================
# Failure -> terminal-error edges: every declared ``* -> FAILED`` transition.
# ===========================================================================


@pytest.mark.integration
async def test_inventorying_to_failed_over_bus(integration_event_bus: Any) -> None:
    """Inventory route raises: IDLE->INVENTORYING->FAILED, terminal final_state
    FAILED with the error surfaced (never a silent 'returned without raising')."""
    bus = integration_event_bus
    await bus.start()
    try:
        calls = _Calls()
        scenario = await _drive(
            bus,
            command=_command("run-inv-fail"),
            inventory=_MockInventory(calls, _inventory((1,)), raises=True),
            triage=_MockTriage(calls, _triage((), EnumPrCategory.GREEN), raises=False),
            reducer=_MockReducer(calls, _reducer({}), raises=False),
            merge=_MockMerge(calls, merged=True),
            fix=_MockFix(calls, raises=False),
            census=_clean_census(),
        )
        assert scenario.edges == (
            ("idle", "inventorying"),
            ("inventorying", "failed"),
        )
        assert scenario.result.final_state == EnumOrchestratorState.FAILED.value
        assert scenario.result.error_message
        assert "inventory boom" in scenario.result.error_message
        assert calls.triage == 0
    finally:
        await bus.close()


@pytest.mark.integration
async def test_triaging_to_failed_over_bus(integration_event_bus: Any) -> None:
    """Reducer route raises while in TRIAGING: TRIAGING->FAILED."""
    bus = integration_event_bus
    await bus.start()
    try:
        calls = _Calls()
        scenario = await _drive(
            bus,
            command=_command("run-tri-fail"),
            inventory=_MockInventory(calls, _inventory((1,)), raises=False),
            triage=_MockTriage(
                calls, _triage((1,), EnumPrCategory.GREEN), raises=False
            ),
            reducer=_MockReducer(
                calls, _reducer({1: EnumReducerIntent.MERGE}), raises=True
            ),
            merge=_MockMerge(calls, merged=True),
            fix=_MockFix(calls, raises=False),
            census=_clean_census(),
        )
        assert scenario.edges == (
            ("idle", "inventorying"),
            ("inventorying", "triaging"),
            ("triaging", "failed"),
        )
        assert scenario.result.final_state == EnumOrchestratorState.FAILED.value
        assert "reducer boom" in (scenario.result.error_message or "")
        assert calls.merge == 0
    finally:
        await bus.close()


@pytest.mark.integration
async def test_fixing_to_failed_over_bus(integration_event_bus: Any) -> None:
    """Fix route raises (ExceptionGroup) while in FIXING: TRIAGING->FIXING->FAILED."""
    bus = integration_event_bus
    await bus.start()
    try:
        calls = _Calls()
        scenario = await _drive(
            bus,
            command=_command("run-fix-fail", fix_only=True),
            inventory=_MockInventory(calls, _inventory((1,)), raises=False),
            triage=_MockTriage(calls, _triage((1,), EnumPrCategory.RED), raises=False),
            reducer=_MockReducer(
                calls, _reducer({1: EnumReducerIntent.FIX}), raises=False
            ),
            merge=_MockMerge(calls, merged=True),
            fix=_MockFix(calls, raises=True),
            census=_clean_census(),
        )
        assert scenario.edges == (
            ("idle", "inventorying"),
            ("inventorying", "triaging"),
            ("triaging", "fixing"),
            ("fixing", "failed"),
        )
        assert scenario.result.final_state == EnumOrchestratorState.FAILED.value
        assert calls.fix == 1
    finally:
        await bus.close()


@pytest.mark.integration
async def test_merging_to_failed_over_bus(integration_event_bus: Any) -> None:
    """A phase-level fault in the MERGING window: MERGING->FAILED.

    ``_call_merge_fanout`` isolates per-PR merge faults, so the declared
    MERGING->FAILED edge is only reachable by a fault in the merge fan-out step
    itself — injected here so the real MERGING->FAILED transition is exercised."""
    bus = integration_event_bus
    await bus.start()
    try:
        calls = _Calls()
        scenario = await _drive(
            bus,
            command=_command("run-merge-fail"),
            inventory=_MockInventory(calls, _inventory((1,)), raises=False),
            triage=_MockTriage(
                calls, _triage((1,), EnumPrCategory.GREEN), raises=False
            ),
            reducer=_MockReducer(
                calls, _reducer({1: EnumReducerIntent.MERGE}), raises=False
            ),
            merge=_MockMerge(calls, merged=True),
            fix=_MockFix(calls, raises=False),
            census=_clean_census(),
            merge_fanout_raises=True,
        )
        assert scenario.edges == (
            ("idle", "inventorying"),
            ("inventorying", "triaging"),
            ("triaging", "merging"),
            ("merging", "failed"),
        )
        assert scenario.result.final_state == EnumOrchestratorState.FAILED.value
    finally:
        await bus.close()


@pytest.mark.integration
async def test_verifying_to_failed_over_bus(integration_event_bus: Any) -> None:
    """A phase-level fault in the VERIFYING window: VERIFYING->FAILED.

    ``_run_verification`` isolates per-PR probe faults, so the declared
    VERIFYING->FAILED edge is reached by a fault in the verification step itself."""
    bus = integration_event_bus
    await bus.start()
    try:
        calls = _Calls()
        scenario = await _drive(
            bus,
            command=_command("run-verify-fail-edge", verify=True),
            inventory=_MockInventory(calls, _inventory((1,)), raises=False),
            triage=_MockTriage(
                calls, _triage((1,), EnumPrCategory.GREEN), raises=False
            ),
            reducer=_MockReducer(
                calls, _reducer({1: EnumReducerIntent.MERGE}), raises=False
            ),
            merge=_MockMerge(calls, merged=True),
            fix=_MockFix(calls, raises=False),
            census=_clean_census(),
            probe_outcome=EnumVerificationOutcome.MERGED,
            run_verification_raises=True,
        )
        assert scenario.edges == (
            ("idle", "inventorying"),
            ("inventorying", "triaging"),
            ("triaging", "verifying"),
            ("verifying", "failed"),
        )
        assert scenario.result.final_state == EnumOrchestratorState.FAILED.value
        assert calls.merge == 0
    finally:
        await bus.close()


@pytest.mark.integration
async def test_post_merge_tail_to_failed_over_bus(integration_event_bus: Any) -> None:
    """A fault in the POST_MERGE_TAIL window: MERGING->POST_MERGE_TAIL->FAILED.

    The only un-isolated operation while state.fsm == POST_MERGE_TAIL is the
    per-merged-PR MERGED ledger write; injecting a fault there exercises the
    declared POST_MERGE_TAIL->FAILED edge through the real FSM machinery."""
    bus = integration_event_bus
    await bus.start()
    try:
        calls = _Calls()
        scenario = await _drive(
            bus,
            command=_command("run-tail-fail"),
            inventory=_MockInventory(calls, _inventory((1,)), raises=False),
            triage=_MockTriage(
                calls, _triage((1,), EnumPrCategory.GREEN), raises=False
            ),
            reducer=_MockReducer(
                calls, _reducer({1: EnumReducerIntent.MERGE}), raises=False
            ),
            merge=_MockMerge(calls, merged=True),
            fix=_MockFix(calls, raises=False),
            census=_clean_census(),
            fault_on_action=EnumOrchestratorAction.MERGE,
        )
        assert scenario.edges == (
            ("idle", "inventorying"),
            ("inventorying", "triaging"),
            ("triaging", "merging"),
            ("merging", "post_merge_tail"),
            ("post_merge_tail", "failed"),
        )
        assert scenario.result.final_state == EnumOrchestratorState.FAILED.value
        # The merge route still fired before the tail-phase fault.
        assert calls.merge == 1
    finally:
        await bus.close()


# ===========================================================================
# Org-wide done-gate (OMN-13318): NOT_DONE downgrade negative control.
# ===========================================================================


@pytest.mark.integration
async def test_org_wide_open_downgrades_complete_to_not_done_over_bus(
    integration_event_bus: Any,
) -> None:
    """The FSM reaches COMPLETE but an org-wide open PR remains: the reported
    terminal final_state is downgraded to NOT_DONE and the remainder is surfaced
    on the terminal event. The FSM edges are unchanged (COMPLETE); only the
    report-level state differs."""
    bus = integration_event_bus
    await bus.start()
    try:
        calls = _Calls()
        census = ModelOrgWideOpenPrInventory(
            open_count=1,
            remainders=(
                ModelOrgWideOpenPrRemainder(
                    repo="OmniNode-ai/omnibase_infra",
                    pr_number=999,
                    title="still open elsewhere",
                    url="https://github.com/OmniNode-ai/omnibase_infra/pull/999",
                ),
            ),
        )
        scenario = await _drive(
            bus,
            command=_command("run-notdone", loop_until_done=False),
            inventory=_MockInventory(calls, _inventory((1,)), raises=False),
            triage=_MockTriage(
                calls, _triage((1,), EnumPrCategory.GREEN), raises=False
            ),
            reducer=_MockReducer(
                calls, _reducer({1: EnumReducerIntent.MERGE}), raises=False
            ),
            merge=_MockMerge(calls, merged=True),
            fix=_MockFix(calls, raises=False),
            census=census,
        )
        assert scenario.edges[-1] == ("post_merge_tail", "complete")
        assert scenario.result.final_state == "NOT_DONE"
        assert scenario.result.org_wide_open_count == 1
        assert len(scenario.result.org_wide_open_remainders) == 1
        assert scenario.result.org_wide_open_remainders[0].pr_number == 999
    finally:
        await bus.close()


# ===========================================================================
# Aggregate coverage: the union of all driven paths enters every declared FSM
# state and fires every declared transition edge (incl every * -> FAILED).
# ===========================================================================


@pytest.mark.integration
async def test_union_of_paths_covers_every_declared_state_and_edge(
    integration_event_bus: Any,
) -> None:
    """Drive one scenario per FSM shape and assert the union of observed edges
    equals the full contract-declared edge set, and that every declared state is
    entered. This is the declared-state ORCHESTRATOR DoD asserted mechanically."""
    # Use the canonical in-memory bus type as a factory so each scenario gets a
    # fresh event history (no cross-scenario bleed) without importing the bus
    # class directly (OMN-8726 forbids that import in integration tests).
    bus_factory = type(integration_event_bus)
    observed_edges: set[tuple[str, str]] = set()
    observed_states: set[str] = set()
    scenarios: list[dict[str, Any]] = [
        # merge -> post_merge_tail -> complete
        {
            "command": _command("u-merge"),
            "triage_cat": EnumPrCategory.GREEN,
            "intents": {1: EnumReducerIntent.MERGE},
        },
        # merge + fix -> post_merge_tail -> fixing -> complete
        {
            "command": _command("u-mergefix"),
            "triage_cat": EnumPrCategory.RED,
            "intents": {1: EnumReducerIntent.MERGE, 2: EnumReducerIntent.FIX},
            "prs": (1, 2),
        },
        # fix_only -> triaging -> fixing -> complete
        {
            "command": _command("u-fixonly", fix_only=True),
            "triage_cat": EnumPrCategory.RED,
            "intents": {1: EnumReducerIntent.FIX},
        },
        # inventory_only -> inventorying -> complete
        {
            "command": _command("u-invonly", inventory_only=True),
            "triage_cat": EnumPrCategory.GREEN,
            "intents": {},
        },
        # dry_run -> triaging -> complete
        {
            "command": _command("u-dry", dry_run=True),
            "triage_cat": EnumPrCategory.GREEN,
            "intents": {1: EnumReducerIntent.MERGE},
        },
        # merge_only -> merging -> post_merge_tail -> complete
        {
            "command": _command("u-mergeonly", merge_only=True),
            "triage_cat": EnumPrCategory.GREEN,
            "intents": {1: EnumReducerIntent.MERGE, 2: EnumReducerIntent.FIX},
            "prs": (1, 2),
        },
        # verify -> triaging -> verifying -> merging -> post_merge_tail -> complete
        {
            "command": _command("u-verify", verify=True),
            "triage_cat": EnumPrCategory.GREEN,
            "intents": {1: EnumReducerIntent.MERGE},
            "probe": EnumVerificationOutcome.MERGED,
        },
        # inventorying -> failed
        {
            "command": _command("u-invfail"),
            "triage_cat": EnumPrCategory.GREEN,
            "intents": {},
            "inv_raises": True,
        },
        # triaging -> failed
        {
            "command": _command("u-trifail"),
            "triage_cat": EnumPrCategory.GREEN,
            "intents": {1: EnumReducerIntent.MERGE},
            "reducer_raises": True,
        },
        # merging -> failed
        {
            "command": _command("u-mergefail"),
            "triage_cat": EnumPrCategory.GREEN,
            "intents": {1: EnumReducerIntent.MERGE},
            "merge_fanout_raises": True,
        },
        # verifying -> failed
        {
            "command": _command("u-verifyfail", verify=True),
            "triage_cat": EnumPrCategory.GREEN,
            "intents": {1: EnumReducerIntent.MERGE},
            "probe": EnumVerificationOutcome.MERGED,
            "run_verification_raises": True,
        },
        # fixing -> failed
        {
            "command": _command("u-fixfail", fix_only=True),
            "triage_cat": EnumPrCategory.RED,
            "intents": {1: EnumReducerIntent.FIX},
            "fix_raises": True,
        },
        # post_merge_tail -> failed
        {
            "command": _command("u-tailfail"),
            "triage_cat": EnumPrCategory.GREEN,
            "intents": {1: EnumReducerIntent.MERGE},
            "fault_on_action": EnumOrchestratorAction.MERGE,
        },
    ]
    for spec in scenarios:
        # A fresh bus per scenario keeps each event history isolated.
        bus = bus_factory(
            environment="integration-test", group="omnimarket-integration"
        )
        await bus.start()
        try:
            prs = spec.get("prs", (1,))
            calls = _Calls()
            scenario = await _drive(
                bus,
                command=spec["command"],
                inventory=_MockInventory(
                    calls, _inventory(prs), raises=spec.get("inv_raises", False)
                ),
                triage=_MockTriage(
                    calls, _triage(prs, spec["triage_cat"]), raises=False
                ),
                reducer=_MockReducer(
                    calls,
                    _reducer(spec["intents"]),
                    raises=spec.get("reducer_raises", False),
                ),
                merge=_MockMerge(calls, merged=True),
                fix=_MockFix(calls, raises=spec.get("fix_raises", False)),
                census=_clean_census(),
                probe_outcome=spec.get("probe"),
                merge_fanout_raises=spec.get("merge_fanout_raises", False),
                run_verification_raises=spec.get("run_verification_raises", False),
                fault_on_action=spec.get("fault_on_action"),
            )
            observed_edges.update(scenario.edges)
            observed_states.update(scenario.states_entered)
        finally:
            await bus.close()

    missing_edges = _DECLARED_EDGES - observed_edges
    assert not missing_edges, f"declared FSM edges never fired: {sorted(missing_edges)}"
    unexpected_edges = observed_edges - _DECLARED_EDGES
    assert not unexpected_edges, (
        f"undeclared FSM edges fired: {sorted(unexpected_edges)}"
    )

    missing_states = _DECLARED_STATES - observed_states
    assert not missing_states, (
        f"declared FSM states never entered: {sorted(missing_states)}"
    )
