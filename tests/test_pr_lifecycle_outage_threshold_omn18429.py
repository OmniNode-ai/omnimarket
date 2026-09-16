# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18429: the outage breaker needs a threshold, and its counter must count.

F-07 (OMN-14774) shipped a breaker that opens on **one** ``GITHUB_API_OUTAGE``
reason code anywhere in the sweep. Measured consequence, run_id
``20260916-090453-b82ab4``: one flaky response among a 56-PR org-wide inventory
withheld every merge, enqueue and rerun for the whole pass, including the single
pull request that pass's own triage had classified green. ``gh api /rate_limit``
was clean at the time and the code host's status page carried no incident
against any relevant component.

Two defects, pinned separately here.

**Blast radius.** One unreliable observation is evidence about ONE pull request,
not about the platform. It makes that pull request's state UNKNOWN, and an
UNKNOWN pull request is not mutated — that part is right and is kept. What it
must not do is decide the other fifty-five. A pass-level trip now requires a
declared threshold: an absolute floor of distinct affected pull requests AND a
declared fraction of the observation window, both carried on the node contract
as typed fields. There is no environment variable anywhere in this path.

**The counter.** ``outage_mutations_withheld`` read ``0`` on a pass that
demonstrably withheld a green pull request, so the field could not tell "nothing
was eligible" from "something was eligible and was withheld" — which is the only
question it exists to answer. The accounting is now computed once, from the
intents the reducer produced, BEFORE the dry-run short-circuit, so a dry run
reports the same number the live pass would have withheld.
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

pytestmark = pytest.mark.unit

_OUTAGE = "github_api_outage"
_REPO = "OmniNode-ai/omnimarket"

# The pass that produced this ticket: one green pull request the triage cleared,
# one whose failed check carried the outage code, and fifty-four the reducer
# skipped. The exact shape that must no longer poison the pass.
_GREEN_PR = 101
_OUTAGE_PR = 102
_SKIPPED_PRS = tuple(range(200, 254))


def _green() -> tuple[PrRecord, TriageRecord, ReducerIntent]:
    return (
        PrRecord(
            pr_number=_GREEN_PR,
            repo=_REPO,
            checks_status="success",
            review_status="approved",
            is_draft=False,
            coderabbit_unresolved=0,
            merge_state_status="CLEAN",
        ),
        TriageRecord(pr_number=_GREEN_PR, repo=_REPO, category=EnumPrCategory.GREEN),
        ReducerIntent(pr_number=_GREEN_PR, repo=_REPO, intent=EnumReducerIntent.MERGE),
    )


def _outage(pr_number: int) -> tuple[PrRecord, TriageRecord, ReducerIntent]:
    return (
        PrRecord(
            pr_number=pr_number,
            repo=_REPO,
            checks_status="failure",
            review_status="pending",
            failed_check_names=("verify / Run Receipt-Gate",),
            failed_check_reason_codes=(_OUTAGE,),
        ),
        TriageRecord(
            pr_number=pr_number,
            repo=_REPO,
            category=EnumPrCategory.RED,
            failed_check_names=("verify / Run Receipt-Gate",),
            failed_check_reason_codes=(_OUTAGE,),
        ),
        ReducerIntent(pr_number=pr_number, repo=_REPO, intent=EnumReducerIntent.FIX),
    )


def _skipped(pr_number: int) -> tuple[PrRecord, TriageRecord, ReducerIntent]:
    return (
        PrRecord(
            pr_number=pr_number,
            repo=_REPO,
            checks_status="failure",
            review_status="pending",
            failed_check_names=("some / product check",),
            failed_check_reason_codes=("product_failure",),
        ),
        TriageRecord(pr_number=pr_number, repo=_REPO, category=EnumPrCategory.RED),
        ReducerIntent(pr_number=pr_number, repo=_REPO, intent=EnumReducerIntent.SKIP),
    )


def _assemble(
    rows: list[tuple[PrRecord, TriageRecord, ReducerIntent]],
) -> tuple[MockInventory, MockTriage, MockReducer]:
    return (
        MockInventory(prs=tuple(r[0] for r in rows)),
        MockTriage(classified=tuple(r[1] for r in rows)),
        MockReducer(intents=tuple(r[2] for r in rows)),
    )


async def _run(
    rows: list[tuple[PrRecord, TriageRecord, ReducerIntent]],
    **command_kwargs: Any,
) -> tuple[Any, MockMerge, MockFix]:
    inventory, triage, reducer = _assemble(rows)
    merge = MockMerge(prs_merged=1)
    fix = MockFix(prs_dispatched=1)
    raw_bus = EventBusInmemory()
    if not raw_bus._started:
        await raw_bus.start()
    orch = _TestOrchestrator(
        _mock_inventory_prs=inventory._prs,
        inventory=inventory,
        triage=triage,
        reducer=reducer,
        merge=merge,
        fix=fix,
        event_bus=cast(ProtocolEventBusPublisher, raw_bus),
        occ_stamp_readback=_LandedStampReadback(),
        outage_recovery_probe=None,
    )
    command = _make_command(
        action_mode=EnumArmActionMode.ENFORCE,
        merge_queue_mutation_kill_switch=False,
        **command_kwargs,
    )
    return await orch.handle(command), merge, fix


def _one_outage_in_a_wide_sweep() -> list[tuple[PrRecord, TriageRecord, ReducerIntent]]:
    return [_green(), _outage(_OUTAGE_PR)] + [_skipped(n) for n in _SKIPPED_PRS]


class TestOneSignalDoesNotDecideThePass:
    """The defect: one unreliable observation withheld fifty-six pull requests."""

    async def test_a_single_outage_signal_leaves_the_breaker_closed(self) -> None:
        result, _merge, _fix = await _run(_one_outage_in_a_wide_sweep())
        assert result.outage_active is False, (
            "One outage-tagged pull request out of 56 is below the declared "
            "floor and below the declared fraction. It is evidence about one "
            "pull request, not about the code host."
        )

    async def test_the_green_pull_request_still_merges(self) -> None:
        result, merge, _fix = await _run(_one_outage_in_a_wide_sweep())
        assert merge.call_count == 1, (
            "The pull request this pass triaged green was withheld by an "
            "unrelated pull request's flaky fetch."
        )
        assert result.prs_merged == 1

    async def test_the_affected_pull_request_is_unknown_and_is_not_mutated(
        self,
    ) -> None:
        """The per-PR half of the verdict, which is correct and is kept."""
        result, _merge, fix = await _run(_one_outage_in_a_wide_sweep())
        assert _OUTAGE_PR not in fix.dispatched_pr_numbers, (
            "A pull request whose state could not be read reliably must not be "
            "mutated, breaker open or closed."
        )
        assert result.outage_prs_unknown == 1

    async def test_the_counter_counts_the_one_withheld_pull_request(self) -> None:
        result, _merge, _fix = await _run(_one_outage_in_a_wide_sweep())
        assert result.outage_mutations_withheld == 1, (
            "The field exists to distinguish 'nothing was eligible' from "
            "'something was eligible and was withheld'. A zero here cannot."
        )


class TestARealOutageStillTripsThePass:
    """Positive control. Without this the first class proves only that the breaker is dead."""

    async def test_a_wide_outage_opens_the_breaker(self) -> None:
        rows = [_green()] + [_outage(n) for n in _SKIPPED_PRS]
        result, merge, fix = await _run(rows)
        assert result.outage_active is True
        assert merge.call_count == 0
        assert fix.call_count == 0
        # 1 merge intent + 54 fix intents, all withheld.
        assert result.outage_mutations_withheld == 1 + len(_SKIPPED_PRS)

    async def test_the_declared_floor_is_what_decides(self) -> None:
        """Three affected of six clears the default floor and fraction; two does not."""
        three = [_green()] + [_outage(n) for n in (301, 302, 303)] + [_skipped(304)]
        two = (
            [_green()]
            + [_outage(n) for n in (301, 302)]
            + [_skipped(n) for n in (303, 304)]
        )
        tripped, _m, _f = await _run(three)
        held, _m2, _f2 = await _run(two)
        assert tripped.outage_active is True
        assert held.outage_active is False

    async def test_the_threshold_is_declared_on_the_command_not_an_env_var(
        self,
    ) -> None:
        """A caller can tighten the floor to one and get the old behaviour back."""
        result, merge, _fix = await _run(
            _one_outage_in_a_wide_sweep(),
            outage_breaker_min_outage_prs=1,
            outage_breaker_min_outage_fraction=0.0,
        )
        assert result.outage_active is True
        assert merge.call_count == 0

    async def test_a_narrow_sweep_falls_back_to_the_floor_alone(self) -> None:
        """Below the declared window minimum a fraction is noise, so only the floor counts."""
        rows = [_outage(n) for n in (301, 302, 303)]
        result, _merge, _fix = await _run(rows)
        assert result.outage_active is True


class TestTheCounterCountsOnADryRun:
    """The measured miscount: the dry-run path returned before any accounting ran."""

    async def test_dry_run_reports_what_the_breaker_would_withhold(self) -> None:
        rows = [_green()] + [_outage(n) for n in _SKIPPED_PRS]
        result, merge, fix = await _run(rows, dry_run=True)
        assert result.outage_active is True
        assert merge.call_count == 0
        assert fix.call_count == 0
        assert result.outage_mutations_withheld == 1 + len(_SKIPPED_PRS), (
            "run_id 20260916-090453-b82ab4 reported outage_mutations_withheld=0 "
            "on exactly this shape, because the dry-run short-circuit returned "
            "before either withholding site could increment."
        )

    async def test_dry_run_with_no_outage_reports_zero(self) -> None:
        """Positive control: the zero has to still be reachable and still mean zero."""
        rows = [_green()] + [_skipped(n) for n in _SKIPPED_PRS]
        result, _merge, _fix = await _run(rows, dry_run=True)
        assert result.outage_active is False
        assert result.outage_mutations_withheld == 0
        assert result.outage_prs_unknown == 0
