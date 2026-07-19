# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14774 (F-07): merge-controller active-outage backoff / circuit breaker.

The merge-check classifier already EMITS ``GITHUB_API_OUTAGE`` (OMN-14765, PR
#1808). These tests prove the orchestrator now CONSUMES that reason code: when a
detected outage trips the circuit breaker, the merge controller **pauses**
REST-dependent mutations (merge / enqueue / rerun) for the pass and only resumes
when a recovery probe passes — fail-closed while the outage window is active.

Acceptance criteria driven here end-to-end through ``handle()`` with the
golden-chain sub-handler doubles:

1. A synthetic GITHUB_API_OUTAGE reason code causes the orchestrator to WITHHOLD
   REST-dependent mutations — no merge / stall-remediation / fix (rerun) call is
   issued during the paused window.
2. Resumption is gated on a recovery-probe pass: probe FAIL keeps the breaker
   open (mutations withheld); probe PASS closes it and resumes.
3. A check classified GITHUB_API_OUTAGE is never routed to a product-code fix
   during the pause (the whole fix dispatch is withheld).
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)

from omnimarket.nodes.node_pr_arm_gate_compute.models.model_arm_gate_policy import (
    EnumArmActionMode,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
    EnumPrCategory,
    EnumReducerIntent,
    PrRecord,
    ReducerIntent,
    TriageRecord,
)

# Reuse the golden-chain harness (mock sub-handlers + gh-CLI-bypassing subclass).
from tests.test_golden_chain_pr_lifecycle_orchestrator import (
    MockFix,
    MockInventory,
    MockMerge,
    MockReducer,
    MockTriage,
    _LandedStampReadback,
    _make_command,
    _TestOrchestrator,
)

_OUTAGE = "github_api_outage"

# A green PR with arm-gate-ready facts (merge intent) — merges when the breaker
# is CLOSED, is withheld when it is OPEN.
_PR_GREEN = PrRecord(
    pr_number=101,
    repo="OmniNode-ai/omnimarket",
    checks_status="success",
    review_status="approved",
    is_draft=False,
    coderabbit_unresolved=0,
    merge_state_status="CLEAN",
)
# A red PR whose failed required check was classified GITHUB_API_OUTAGE — this is
# the sweep-wide outage signal that trips the breaker. It carries a FIX intent so
# we can assert the fix dispatch (reruns) is withheld during the pause.
_PR_OUTAGE = PrRecord(
    pr_number=102,
    repo="OmniNode-ai/omnimarket",
    checks_status="failure",
    review_status="pending",
    failed_check_names=("verify / Run Receipt-Gate",),
    failed_check_reason_codes=(_OUTAGE,),
)
_TRIAGE_GREEN = TriageRecord(
    pr_number=101, repo="OmniNode-ai/omnimarket", category=EnumPrCategory.GREEN
)
_TRIAGE_OUTAGE = TriageRecord(
    pr_number=102,
    repo="OmniNode-ai/omnimarket",
    category=EnumPrCategory.RED,
    failed_check_names=("verify / Run Receipt-Gate",),
    failed_check_reason_codes=(_OUTAGE,),
)
_INTENT_MERGE = ReducerIntent(
    pr_number=101, repo="OmniNode-ai/omnimarket", intent=EnumReducerIntent.MERGE
)
_INTENT_FIX = ReducerIntent(
    pr_number=102, repo="OmniNode-ai/omnimarket", intent=EnumReducerIntent.FIX
)


async def _build_orch(
    *,
    inventory: MockInventory,
    triage: MockTriage,
    reducer: MockReducer,
    merge: MockMerge,
    fix: MockFix,
    outage_recovery_probe: Any = None,
) -> _TestOrchestrator:
    """Construct the gh-bypassing test orchestrator, forwarding the outage probe.

    Mirrors the golden-chain ``_make_orchestrator`` helper but threads
    ``outage_recovery_probe`` through to the real handler constructor (the
    golden-chain helper does not expose it).
    """
    raw_bus = EventBusInmemory()
    if not raw_bus._started:
        await raw_bus.start()
    bus = cast(ProtocolEventBusPublisher, raw_bus)
    return _TestOrchestrator(
        _mock_inventory_prs=inventory._prs,
        inventory=inventory,
        triage=triage,
        reducer=reducer,
        merge=merge,
        fix=fix,
        event_bus=bus,
        occ_stamp_readback=_LandedStampReadback(),
        outage_recovery_probe=outage_recovery_probe,
    )


def _fixtures() -> tuple[MockInventory, MockTriage, MockReducer, MockMerge, MockFix]:
    inventory = MockInventory(prs=(_PR_GREEN, _PR_OUTAGE))
    triage = MockTriage(classified=(_TRIAGE_GREEN, _TRIAGE_OUTAGE))
    reducer = MockReducer(intents=(_INTENT_MERGE, _INTENT_FIX))
    merge = MockMerge(prs_merged=1)
    fix = MockFix(prs_dispatched=1)
    return inventory, triage, reducer, merge, fix


def _command(**kwargs: object) -> object:
    return _make_command(
        action_mode=EnumArmActionMode.ENFORCE,
        merge_queue_mutation_kill_switch=False,
        **kwargs,
    )


@pytest.mark.unit
class TestOutageBreakerWithholdsMutations:
    """Acceptance 1 + 3: an outage tag pauses merge / stall / fix mutations."""

    async def test_outage_withholds_merge_and_fix(self) -> None:
        inventory, triage, reducer, merge, fix = _fixtures()
        orch = await _build_orch(
            inventory=inventory,
            triage=triage,
            reducer=reducer,
            merge=merge,
            fix=fix,
            outage_recovery_probe=None,  # no in-pass recovery -> stays paused
        )
        result = await orch.handle(_command())

        # Acceptance 1: no REST-dependent mutation issued during the pause.
        assert merge.call_count == 0
        assert fix.call_count == 0
        # Acceptance 3: the outage-classified check was never routed to a
        # product-code fix — the fix dispatch is withheld wholesale.
        assert fix.dispatched_pr_numbers == []
        # Breaker state surfaced on the result.
        assert result.outage_active is True
        # Both acted-on PRs (1 merge + 1 fix) were withheld.
        assert result.outage_mutations_withheld == 2
        assert result.prs_merged == 0
        assert result.prs_fixed == 0

    async def test_outage_withholds_stall_remediation(self) -> None:
        from types import SimpleNamespace

        stuck_entry = SimpleNamespace(
            repo="OmniNode-ai/omnimarket",
            pr_number=101,
            queue_state="AWAITING_CHECKS",
            merge_group_run_count=0,
        )
        inventory = MockInventory(prs=(_PR_OUTAGE,), stuck_queue_prs=(stuck_entry,))
        triage = MockTriage(classified=(_TRIAGE_OUTAGE,))
        reducer = MockReducer(intents=(_INTENT_FIX,))
        orch = await _build_orch(
            inventory=inventory,
            triage=triage,
            reducer=reducer,
            merge=MockMerge(),
            fix=MockFix(),
            outage_recovery_probe=None,
        )
        # enable_stall_remediation would normally dequeue+re-enqueue the stuck PR.
        result = await orch.handle(_command(enable_stall_remediation=True))

        # The enqueue mutation must NOT fire during the outage window.
        assert orch.queue_stall_adapter.calls == []
        assert result.outage_active is True


@pytest.mark.unit
class TestOutageBreakerRecoveryProbe:
    """Acceptance 2: resumption is gated on a recovery-probe pass."""

    async def test_probe_pass_resumes_mutations(self) -> None:
        inventory, triage, reducer, merge, fix = _fixtures()
        orch = await _build_orch(
            inventory=inventory,
            triage=triage,
            reducer=reducer,
            merge=merge,
            fix=fix,
            outage_recovery_probe=lambda: True,  # API recovered -> resume
        )
        result = await orch.handle(_command())

        # Breaker closed by the passing probe: mutations resume this same pass.
        assert result.outage_active is False
        assert result.outage_mutations_withheld == 0
        assert merge.call_count == 1
        assert result.prs_merged == 1
        assert fix.call_count == 1
        assert result.prs_fixed == 1

    async def test_probe_fail_keeps_breaker_open(self) -> None:
        inventory, triage, reducer, merge, fix = _fixtures()
        orch = await _build_orch(
            inventory=inventory,
            triage=triage,
            reducer=reducer,
            merge=merge,
            fix=fix,
            outage_recovery_probe=lambda: False,  # API still down -> stay paused
        )
        result = await orch.handle(_command())

        assert result.outage_active is True
        assert merge.call_count == 0
        assert fix.call_count == 0
        assert result.outage_mutations_withheld == 2


@pytest.mark.unit
class TestNoOutageIsUnchanged:
    """Control: with no outage code the breaker never engages (regression guard)."""

    async def test_no_outage_merges_and_fixes_normally(self) -> None:
        green = PrRecord(
            pr_number=101,
            repo="OmniNode-ai/omnimarket",
            checks_status="success",
            review_status="approved",
            is_draft=False,
            coderabbit_unresolved=0,
            merge_state_status="CLEAN",
        )
        red = PrRecord(
            pr_number=102,
            repo="OmniNode-ai/omnimarket",
            checks_status="failure",
            failed_check_names=("ruff / lint",),
            failed_check_reason_codes=("product_failed",),
        )
        inventory = MockInventory(prs=(green, red))
        triage = MockTriage(
            classified=(
                _TRIAGE_GREEN,
                TriageRecord(
                    pr_number=102,
                    repo="OmniNode-ai/omnimarket",
                    category=EnumPrCategory.RED,
                    failed_check_reason_codes=("product_failed",),
                ),
            )
        )
        reducer = MockReducer(intents=(_INTENT_MERGE, _INTENT_FIX))
        merge = MockMerge(prs_merged=1)
        fix = MockFix(prs_dispatched=1)
        orch = await _build_orch(
            inventory=inventory,
            triage=triage,
            reducer=reducer,
            merge=merge,
            fix=fix,
            outage_recovery_probe=None,
        )
        result = await orch.handle(_command())

        assert result.outage_active is False
        assert result.outage_mutations_withheld == 0
        assert merge.call_count == 1
        assert fix.call_count == 1
        assert result.prs_merged == 1
        assert result.prs_fixed == 1
