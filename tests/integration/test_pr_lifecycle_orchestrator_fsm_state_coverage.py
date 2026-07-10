# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared-state FSM coverage for node_pr_lifecycle_orchestrator (OMN-13674).

ORCHESTRATOR archetype. This suite drives the real
``HandlerPrLifecycleOrchestrator`` over the canonical in-memory bus
(``EventBusInmemory`` via the ``integration_event_bus`` fixture +
``LocalRuntimeBusAdapter``) and proves, mechanically, that:

* every declared FSM state in ``contract.yaml`` (IDLE, INVENTORYING, TRIAGING,
  VERIFYING, MERGING, POST_MERGE_TAIL, FIXING, COMPLETE, FAILED) is entered;
* every declared transition edge fires — including the failure→FAILED edges
  from every non-terminal phase; and
* the correct terminal-event payload (``final_state`` COMPLETE / FAILED plus the
  typed counters) lands on the contract ``terminal_event`` topic per path.

Observed edges are read back off the bus from the contract-declared
``pr-lifecycle-orchestrator-phase-transition`` topic — the orchestrator publishes
one phase event per FSM transition — so the coverage claim is proven from durable
bus evidence, not from inspecting handler internals.

The gh/git boundary is replaced by the orchestrator's overridable enumeration +
verification hooks and injected ``_Mock*`` sub-handlers. Error edges are exercised
by injecting a deterministic fault at the phase-entry call for the target phase
(constructor/subclass injection — never subprocess/asyncpg monkeypatch). No real
prod-mutating effect is ever executed.

Related: OMN-13674 (node state-coverage), OMN-12570 (phase separation),
OMN-13673 (verify gate).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_pr_arm_gate_compute.models.model_arm_gate_policy import (
    EnumArmActionMode,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    TOPIC_COMPLETED,
    TOPIC_PHASE_TRANSITION,
    HandlerPrLifecycleOrchestrator,
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
    EnumVerificationTarget,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

_START_TOPIC = "onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1"
_REPO = "OmniNode-ai/omnimarket"

# ---------------------------------------------------------------------------
# Declared FSM surface (mirrors node_pr_lifecycle_orchestrator/contract.yaml).
# The test asserts the union of the scenarios below reproduces this set exactly,
# so drift between the contract and the covered edges fails the suite.
# ---------------------------------------------------------------------------
_DECLARED_STATES: frozenset[str] = frozenset(
    {
        "IDLE",
        "INVENTORYING",
        "TRIAGING",
        "VERIFYING",
        "MERGING",
        "POST_MERGE_TAIL",
        "FIXING",
        "COMPLETE",
        "FAILED",
    }
)

_DECLARED_EDGES: frozenset[tuple[str, str]] = frozenset(
    {
        ("IDLE", "INVENTORYING"),
        ("INVENTORYING", "TRIAGING"),
        ("INVENTORYING", "COMPLETE"),
        ("TRIAGING", "MERGING"),
        ("TRIAGING", "FIXING"),
        ("TRIAGING", "COMPLETE"),
        ("TRIAGING", "VERIFYING"),
        ("VERIFYING", "MERGING"),
        ("VERIFYING", "FAILED"),
        ("MERGING", "POST_MERGE_TAIL"),
        ("POST_MERGE_TAIL", "FIXING"),
        ("POST_MERGE_TAIL", "COMPLETE"),
        ("POST_MERGE_TAIL", "FAILED"),
        ("FIXING", "COMPLETE"),
        ("INVENTORYING", "FAILED"),
        ("TRIAGING", "FAILED"),
        ("MERGING", "FAILED"),
        ("FIXING", "FAILED"),
    }
)


# ---------------------------------------------------------------------------
# Injected sub-handlers (_Mock* pattern — constructor injection).
# ---------------------------------------------------------------------------
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


class _MergeOutcome:
    """Per-PR merge result — the orchestrator reads ``.merged``."""

    def __init__(self, merged: bool) -> None:
        self.merged = merged


class _MockMerge:
    def __init__(self, merged: bool = True) -> None:
        self._merged = merged

    async def handle(self, command: Any) -> _MergeOutcome:
        return _MergeOutcome(self._merged)


class _MockFix:
    async def handle(self, command: Any) -> FixResult:
        return FixResult(prs_dispatched=1, prs_skipped=0)


class _InjectedFaultError(RuntimeError):
    """Deterministic fault injected at a phase-entry boundary to drive X→FAILED."""


class _VerifiedOccStampReadback:
    """Hermetic OCC-companion read-back stub (OMN-14151 arm-gate).

    Always verifies so the arm-gate's occ_companion_verified criterion is
    satisfied without any live gh/remote call.
    """

    async def verify_fix_landed(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> ModelOccStampReadbackResult:
        return ModelOccStampReadbackResult(verified=True, reason="test: stubbed")


class _FsmCoverageOrchestrator(HandlerPrLifecycleOrchestrator):
    """Orchestrator wired for deterministic FSM coverage over the bus.

    * ``_enumerate_*`` return the synthetic PR set (no gh CLI).
    * ``_verification_target_for`` returns a real (non-skip) target so the verify
      probe runs; ``_execute_verification_probe`` returns the injected outcome.
    * ``fault`` names the phase-entry call to raise at, driving that phase's
      declared error edge into FAILED. ``None`` runs the happy path.
    * result-file / OCC-edge writes are no-ops (no filesystem dependency).
    """

    def __init__(
        self,
        *,
        mock_prs: tuple[PrRecord, ...] = (),
        verification_outcome: EnumVerificationOutcome | None = None,
        fault: str | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("occ_stamp_readback", _VerifiedOccStampReadback())
        super().__init__(**kwargs)
        self._mock_prs = mock_prs
        self._verification_outcome = verification_outcome
        self._fault = fault

    # --- gh/git boundary replacement ---
    def _enumerate_repos(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(pr.repo for pr in self._mock_prs))

    def _enumerate_open_pr_numbers(self, repo: str) -> tuple[int, ...]:
        return tuple(pr.pr_number for pr in self._mock_prs if pr.repo == repo)

    def _verification_target_for(
        self, repo: str, pr_number: int
    ) -> EnumVerificationTarget:
        return EnumVerificationTarget.RUNTIME_HEALTH

    async def _execute_verification_probe(  # type: ignore[override]
        self, *, target: Any, timeout_seconds: int
    ) -> EnumVerificationOutcome:
        assert self._verification_outcome is not None
        return self._verification_outcome

    def _write_result_file(self, run_id: str, result: Any) -> None:
        return None

    def _write_occ_dependency_edges_file(self, run_id: str, edges: Any) -> None:
        return None

    # --- phase-entry fault injection (drives each declared X→FAILED edge) ---
    async def _call_inventory(self, **kwargs: Any) -> Any:
        if self._fault == "inventory":
            raise _InjectedFaultError("inventory boom")
        return await super()._call_inventory(**kwargs)

    async def _call_triage(self, **kwargs: Any) -> Any:
        if self._fault == "triage":
            raise _InjectedFaultError("triage boom")
        return await super()._call_triage(**kwargs)

    async def _run_verification(self, **kwargs: Any) -> Any:
        if self._fault == "verify":
            raise _InjectedFaultError("verify boom")
        return await super()._run_verification(**kwargs)

    async def _call_merge_fanout(self, **kwargs: Any) -> Any:
        if self._fault == "merge":
            raise _InjectedFaultError("merge boom")
        return await super()._call_merge_fanout(**kwargs)

    async def _dispatch_fix_parallel(self, **kwargs: Any) -> Any:
        if self._fault == "fix":
            raise _InjectedFaultError("fix boom")
        return await super()._dispatch_fix_parallel(**kwargs)

    def _record_ledger_event(self, **kwargs: Any) -> Any:
        # POST_MERGE_TAIL records the terminal MERGED conclusion (action=MERGE).
        # Raising there drives the POST_MERGE_TAIL→FAILED edge specifically.
        if self._fault == "post_merge_tail":
            from omnimarket.nodes.pr_ledger_native import EnumOrchestratorAction

            if kwargs.get("orchestrator_action") == EnumOrchestratorAction.MERGE:
                raise _InjectedFaultError("post-merge-tail boom")
        return super()._record_ledger_event(**kwargs)


class _TypedHandlerWrapper:
    """Bridge adapter kwargs into the orchestrator's typed command API."""

    def __init__(self, handler: HandlerPrLifecycleOrchestrator) -> None:
        self._handler = handler

    async def handle(self, **payload: Any) -> Any:
        return await self._handler.handle(ModelPrLifecycleStartCommand(**payload))


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------
def _green(pr_number: int) -> PrRecord:
    return PrRecord(
        pr_number=pr_number,
        repo=_REPO,
        checks_status="success",
        # OMN-14151: arm-gate-ready facts so scenarios asserting a real merge
        # continue to pass under the merge-queue governor's fail-closed arm
        # gate. Harmless for scenarios that never reach MERGING.
        is_draft=False,
        coderabbit_unresolved=0,
        merge_state_status="CLEAN",
    )


def _red(pr_number: int) -> PrRecord:
    return PrRecord(pr_number=pr_number, repo=_REPO, checks_status="failure")


def _triage(pr_number: int, category: EnumPrCategory) -> TriageRecord:
    return TriageRecord(pr_number=pr_number, repo=_REPO, category=category)


def _intent(pr_number: int, intent: EnumReducerIntent) -> ReducerIntent:
    return ReducerIntent(pr_number=pr_number, repo=_REPO, intent=intent)


@dataclass(frozen=True)
class _Scenario:
    id: str
    prs: tuple[PrRecord, ...]
    triage: tuple[TriageRecord, ...]
    intents: tuple[ReducerIntent, ...]
    command_kwargs: dict[str, Any]
    expected_edges: frozenset[tuple[str, str]]
    expected_final_state: str
    verify_outcome: EnumVerificationOutcome | None = None
    fault: str | None = None
    expected_counters: dict[str, int] = field(default_factory=dict)


_SCENARIOS: tuple[_Scenario, ...] = (
    # --- happy paths ---
    _Scenario(
        id="inventory-only",
        prs=(_green(101),),
        triage=(_triage(101, EnumPrCategory.GREEN),),
        intents=(_intent(101, EnumReducerIntent.MERGE),),
        command_kwargs={"inventory_only": True},
        expected_edges=frozenset(
            {("IDLE", "INVENTORYING"), ("INVENTORYING", "COMPLETE")}
        ),
        expected_final_state="COMPLETE",
        expected_counters={"prs_inventoried": 1, "prs_merged": 0, "prs_fixed": 0},
    ),
    _Scenario(
        id="fix-only",
        prs=(_red(102),),
        triage=(_triage(102, EnumPrCategory.RED),),
        intents=(_intent(102, EnumReducerIntent.FIX),),
        command_kwargs={"fix_only": True},
        expected_edges=frozenset(
            {
                ("IDLE", "INVENTORYING"),
                ("INVENTORYING", "TRIAGING"),
                ("TRIAGING", "FIXING"),
                ("FIXING", "COMPLETE"),
            }
        ),
        expected_final_state="COMPLETE",
        expected_counters={"prs_fixed": 1, "prs_merged": 0},
    ),
    _Scenario(
        id="merge-only",
        prs=(_green(103),),
        triage=(_triage(103, EnumPrCategory.GREEN),),
        intents=(_intent(103, EnumReducerIntent.MERGE),),
        command_kwargs={
            "merge_only": True,
            "action_mode": EnumArmActionMode.ENFORCE,
            "merge_queue_mutation_kill_switch": False,
        },
        expected_edges=frozenset(
            {
                ("IDLE", "INVENTORYING"),
                ("INVENTORYING", "TRIAGING"),
                ("TRIAGING", "MERGING"),
                ("MERGING", "POST_MERGE_TAIL"),
                ("POST_MERGE_TAIL", "COMPLETE"),
            }
        ),
        expected_final_state="COMPLETE",
        expected_counters={"prs_merged": 1, "prs_fixed": 0},
    ),
    _Scenario(
        id="mixed-merge-then-fix",
        prs=(_green(104), _red(105)),
        triage=(
            _triage(104, EnumPrCategory.GREEN),
            _triage(105, EnumPrCategory.RED),
        ),
        intents=(
            _intent(104, EnumReducerIntent.MERGE),
            _intent(105, EnumReducerIntent.FIX),
        ),
        command_kwargs={
            "action_mode": EnumArmActionMode.ENFORCE,
            "merge_queue_mutation_kill_switch": False,
        },
        expected_edges=frozenset(
            {
                ("IDLE", "INVENTORYING"),
                ("INVENTORYING", "TRIAGING"),
                ("TRIAGING", "MERGING"),
                ("MERGING", "POST_MERGE_TAIL"),
                ("POST_MERGE_TAIL", "FIXING"),
                ("FIXING", "COMPLETE"),
            }
        ),
        expected_final_state="COMPLETE",
        expected_counters={"prs_merged": 1, "prs_fixed": 1},
    ),
    _Scenario(
        id="verify-pass-then-merge",
        prs=(_green(106),),
        triage=(_triage(106, EnumPrCategory.GREEN),),
        intents=(_intent(106, EnumReducerIntent.MERGE),),
        command_kwargs={
            "verify": True,
            "action_mode": EnumArmActionMode.ENFORCE,
            "merge_queue_mutation_kill_switch": False,
        },
        verify_outcome=EnumVerificationOutcome.MERGED,
        expected_edges=frozenset(
            {
                ("IDLE", "INVENTORYING"),
                ("INVENTORYING", "TRIAGING"),
                ("TRIAGING", "VERIFYING"),
                ("VERIFYING", "MERGING"),
                ("MERGING", "POST_MERGE_TAIL"),
                ("POST_MERGE_TAIL", "COMPLETE"),
            }
        ),
        expected_final_state="COMPLETE",
        expected_counters={"prs_verified": 1, "prs_merged": 1},
    ),
    _Scenario(
        id="dry-run-no-actionable",
        prs=(_green(107),),
        triage=(_triage(107, EnumPrCategory.GREEN),),
        intents=(_intent(107, EnumReducerIntent.MERGE),),
        command_kwargs={"dry_run": True},
        expected_edges=frozenset(
            {
                ("IDLE", "INVENTORYING"),
                ("INVENTORYING", "TRIAGING"),
                ("TRIAGING", "COMPLETE"),
            }
        ),
        expected_final_state="COMPLETE",
        expected_counters={"prs_merged": 0, "prs_fixed": 0},
    ),
    # --- failure edges (fault injected at the target phase-entry boundary) ---
    _Scenario(
        id="inventory-error",
        prs=(_green(111),),
        triage=(_triage(111, EnumPrCategory.GREEN),),
        intents=(_intent(111, EnumReducerIntent.MERGE),),
        command_kwargs={},
        fault="inventory",
        expected_edges=frozenset(
            {("IDLE", "INVENTORYING"), ("INVENTORYING", "FAILED")}
        ),
        expected_final_state="FAILED",
    ),
    _Scenario(
        id="triage-error",
        prs=(_green(112),),
        triage=(_triage(112, EnumPrCategory.GREEN),),
        intents=(_intent(112, EnumReducerIntent.MERGE),),
        command_kwargs={},
        fault="triage",
        expected_edges=frozenset(
            {
                ("IDLE", "INVENTORYING"),
                ("INVENTORYING", "TRIAGING"),
                ("TRIAGING", "FAILED"),
            }
        ),
        expected_final_state="FAILED",
    ),
    _Scenario(
        id="verify-error",
        prs=(_green(113),),
        triage=(_triage(113, EnumPrCategory.GREEN),),
        intents=(_intent(113, EnumReducerIntent.MERGE),),
        command_kwargs={"verify": True},
        verify_outcome=EnumVerificationOutcome.MERGED,
        fault="verify",
        expected_edges=frozenset(
            {
                ("IDLE", "INVENTORYING"),
                ("INVENTORYING", "TRIAGING"),
                ("TRIAGING", "VERIFYING"),
                ("VERIFYING", "FAILED"),
            }
        ),
        expected_final_state="FAILED",
    ),
    _Scenario(
        id="merge-error",
        prs=(_green(114),),
        triage=(_triage(114, EnumPrCategory.GREEN),),
        intents=(_intent(114, EnumReducerIntent.MERGE),),
        command_kwargs={"merge_only": True},
        fault="merge",
        expected_edges=frozenset(
            {
                ("IDLE", "INVENTORYING"),
                ("INVENTORYING", "TRIAGING"),
                ("TRIAGING", "MERGING"),
                ("MERGING", "FAILED"),
            }
        ),
        expected_final_state="FAILED",
    ),
    _Scenario(
        id="post-merge-tail-error",
        prs=(_green(115),),
        triage=(_triage(115, EnumPrCategory.GREEN),),
        intents=(_intent(115, EnumReducerIntent.MERGE),),
        command_kwargs={"merge_only": True},
        fault="post_merge_tail",
        expected_edges=frozenset(
            {
                ("IDLE", "INVENTORYING"),
                ("INVENTORYING", "TRIAGING"),
                ("TRIAGING", "MERGING"),
                ("MERGING", "POST_MERGE_TAIL"),
                ("POST_MERGE_TAIL", "FAILED"),
            }
        ),
        expected_final_state="FAILED",
    ),
    _Scenario(
        id="fix-error",
        prs=(_red(116),),
        triage=(_triage(116, EnumPrCategory.RED),),
        intents=(_intent(116, EnumReducerIntent.FIX),),
        command_kwargs={"fix_only": True},
        fault="fix",
        expected_edges=frozenset(
            {
                ("IDLE", "INVENTORYING"),
                ("INVENTORYING", "TRIAGING"),
                ("TRIAGING", "FIXING"),
                ("FIXING", "FAILED"),
            }
        ),
        expected_final_state="FAILED",
    ),
)


async def _drive_scenario(
    bus: Any, scenario: _Scenario
) -> tuple[set[tuple[str, str]], dict[str, Any]]:
    """Run one scenario end-to-end over the bus; return (observed_edges, terminal_payload)."""
    orchestrator = _FsmCoverageOrchestrator(
        mock_prs=scenario.prs,
        inventory=_MockInventory(scenario.prs),
        triage=_MockTriage(scenario.triage),
        reducer=_MockReducer(scenario.intents),
        merge=_MockMerge(True),
        fix=_MockFix(),
        event_bus=bus,
        verification_outcome=scenario.verify_outcome,
        fault=scenario.fault,
    )
    adapter = LocalRuntimeBusAdapter(
        handler=_TypedHandlerWrapper(orchestrator),
        handler_name="pr-lifecycle-orchestrator",
        input_model_cls=ModelPrLifecycleStartCommand,
        output_topic=TOPIC_COMPLETED,
        bus=bus,
    )
    await bus.subscribe(
        _START_TOPIC,
        on_message=adapter.on_message,
        group_id=f"omn-13674-fsm-{scenario.id}",
    )

    command = ModelPrLifecycleStartCommand(
        correlation_id=uuid4(),
        run_id=f"omn-13674-fsm-{scenario.id}",
        **scenario.command_kwargs,
    )
    await bus.publish(
        _START_TOPIC,
        key=None,
        value=command.model_dump_json().encode("utf-8"),
    )

    # Observed edges: read the contract-declared phase-transition topic off the bus.
    phase_history = await bus.get_event_history(topic=TOPIC_PHASE_TRANSITION)
    observed: set[tuple[str, str]] = set()
    for evt in phase_history:
        body = json.loads(evt.value)
        observed.add((body["from_phase"].upper(), body["to_phase"].upper()))

    completed = await bus.get_event_history(topic=TOPIC_COMPLETED)
    assert len(completed) == 1, f"{scenario.id}: expected exactly 1 terminal event"
    terminal = json.loads(completed[0].value)
    assert terminal["correlation_id"] == str(command.correlation_id)
    return observed, terminal


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", _SCENARIOS, ids=lambda s: s.id)
async def test_scenario_produces_declared_edges_and_terminal(
    integration_event_bus: Any,
    scenario: _Scenario,
) -> None:
    """Each scenario drives its exact declared edges + terminal payload over the bus."""
    await integration_event_bus.start()
    try:
        observed, terminal = await _drive_scenario(integration_event_bus, scenario)

        missing = scenario.expected_edges - observed
        assert not missing, (
            f"{scenario.id}: expected phase edges not observed on the bus: {missing} "
            f"(observed={sorted(observed)})"
        )
        assert terminal["final_state"] == scenario.expected_final_state, (
            f"{scenario.id}: final_state={terminal['final_state']!r}"
        )
        for key, value in scenario.expected_counters.items():
            assert terminal[key] == value, (
                f"{scenario.id}: {key}={terminal[key]!r}, expected {value!r}"
            )
    finally:
        await integration_event_bus.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scenarios_cover_every_declared_state_and_edge(
    integration_event_bus: Any,
) -> None:
    """The union of scenario edges reproduces the contract FSM exactly.

    This ties the per-scenario bus evidence to the declared FSM surface: every
    declared state is entered and every declared transition edge fires across the
    suite — with no undeclared edge slipping in.

    Each scenario needs its own bus (histories must not bleed across runs); fresh
    instances are minted from the fixture's class so this file never imports
    ``EventBusInmemory`` directly (OMN-8726 integration-test guard).
    """
    bus_cls = type(integration_event_bus)

    all_edges: set[tuple[str, str]] = set()
    for scenario in _SCENARIOS:
        bus = bus_cls(environment="integration-test", group="omnimarket-integration")
        await bus.start()
        try:
            observed, _ = await _drive_scenario(bus, scenario)
        finally:
            await bus.close()
        all_edges |= observed

    # No undeclared edge appeared, and every declared edge fired.
    assert all_edges == set(_DECLARED_EDGES), (
        f"edge coverage mismatch: "
        f"missing={set(_DECLARED_EDGES) - all_edges}, "
        f"undeclared={all_edges - set(_DECLARED_EDGES)}"
    )

    entered_states = {frm for frm, _ in all_edges} | {to for _, to in all_edges}
    assert entered_states == set(_DECLARED_STATES), (
        f"state coverage mismatch: "
        f"missing={set(_DECLARED_STATES) - entered_states}, "
        f"extra={entered_states - set(_DECLARED_STATES)}"
    )
