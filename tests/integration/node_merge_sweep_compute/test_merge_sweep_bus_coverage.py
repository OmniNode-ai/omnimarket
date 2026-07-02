# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full declared-state COMPUTE coverage for node_merge_sweep_compute, driven over
the canonical in-memory bus.

OMN-13674 (cluster merge_sweep_pr_lifecycle_compute). The COMPUTE handler
``NodeMergeSweep`` is dispatched through ``LocalRuntimeBusAdapter`` over
``EventBusInmemory`` (via the ``integration_event_bus`` fixture): a
``ModelMergeSweepRequest`` lands on the contract-declared command topic
``onex.cmd.omnimarket.merge-sweep-start.v1`` and the runtime auto-emits the
``ModelMergeSweepResult`` onto the contract-declared terminal topic
``onex.evt.omnimarket.merge-sweep-completed.v1``.

COMPUTE DoD:
  * every declared classification track reached (A-update / A / A-resolve /
    B / skip) and asserted on the terminal-event payload;
  * every mode/flag branch exercised: require_approval on/off, max_total_merges
    cap, skip_polish, use_lifecycle_ordering reorder, and the failure-history
    escalation ladder (STUCK / CHRONIC / RECIDIVIST) in the summary;
  * a negative control: a known-bad (conflicting) PR MUST land in Track B and
    MUST NOT be reported merge-ready (Track A).

Zero network calls: the classifier is pure and the lifecycle-ordering path runs
its own nested in-memory bus; nothing touches GitHub.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.nodes.node_merge_sweep_compute.handlers.handler_merge_sweep import (
    EnumFailureCategory,
    EnumPRTrack,
    ModelFailureHistoryEntry,
    ModelMergeSweepRequest,
    ModelMergeSweepResult,
    ModelPRInfo,
    NodeMergeSweep,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

# Contract-declared topics (node_merge_sweep_compute/contract.yaml).
_START_TOPIC = "onex.cmd.omnimarket.merge-sweep-start.v1"
_COMPLETED_TOPIC = "onex.evt.omnimarket.merge-sweep-completed.v1"


async def _run_over_bus(
    bus: Any, request: ModelMergeSweepRequest
) -> ModelMergeSweepResult:
    """Publish a merge-sweep request onto the declared command topic and return
    the terminal ``ModelMergeSweepResult`` parsed off the declared terminal topic."""
    adapter = LocalRuntimeBusAdapter(
        handler=NodeMergeSweep(),
        handler_name="merge-sweep-compute",
        input_model_cls=ModelMergeSweepRequest,
        output_topic=_COMPLETED_TOPIC,
        bus=bus,
    )
    await bus.subscribe(
        _START_TOPIC,
        on_message=adapter.on_message,
        group_id="omnimarket-merge-sweep-test",
    )
    await bus.publish(
        _START_TOPIC,
        key=None,
        value=request.model_dump_json().encode("utf-8"),
    )
    history = await bus.get_event_history(topic=_COMPLETED_TOPIC)
    assert len(history) == 1, f"expected 1 terminal event on {_COMPLETED_TOPIC}"
    return ModelMergeSweepResult.model_validate(json.loads(history[-1].value))


def _pr(number: int = 1, **overrides: Any) -> ModelPRInfo:
    base: dict[str, Any] = {
        "number": number,
        "title": f"feat: change {number}",
        "repo": "OmniNode-ai/omnimarket",
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "required_checks_pass": True,
    }
    base.update(overrides)
    return ModelPRInfo(**base)


# (pr, expected track, expected failure-category or None)
_CASES = [
    pytest.param(_pr(is_draft=True), EnumPRTrack.SKIP, None, id="draft-skip"),
    pytest.param(
        _pr(merge_state_status="BEHIND"),
        EnumPRTrack.A_UPDATE,
        EnumFailureCategory.BRANCH_STALE,
        id="behind-a-update",
    ),
    pytest.param(
        _pr(mergeable="UNKNOWN"),
        EnumPRTrack.A_UPDATE,
        EnumFailureCategory.BRANCH_STALE,
        id="unknown-mergeable-a-update",
    ),
    pytest.param(
        _pr(review_decision="APPROVED"),
        EnumPRTrack.A_MERGE,
        None,
        id="approved-a-merge",
    ),
    pytest.param(
        _pr(review_decision=None, required_approving_review_count=0),
        EnumPRTrack.A_MERGE,
        None,
        id="solo-dev-no-approval-required-a-merge",
    ),
    pytest.param(
        _pr(
            merge_state_status="BLOCKED",
            review_decision="APPROVED",
            review_bot_gate_passed=None,
        ),
        EnumPRTrack.A_RESOLVE,
        EnumFailureCategory.THREADS_BLOCKED,
        id="blocked-threads-a-resolve",
    ),
    pytest.param(
        _pr(review_bot_gate_passed=False, review_decision="APPROVED"),
        EnumPRTrack.A_RESOLVE,
        EnumFailureCategory.THREADS_BLOCKED,
        id="review-bot-gate-failed-a-resolve",
    ),
    pytest.param(
        _pr(mergeable="CONFLICTING"),
        EnumPRTrack.B_POLISH,
        EnumFailureCategory.CONFLICT,
        id="conflicting-b-polish",
    ),
    pytest.param(
        _pr(
            required_checks_pass=False,
            required_checks_failed=True,
            review_decision="APPROVED",
        ),
        EnumPRTrack.B_POLISH,
        EnumFailureCategory.CI_TEST,
        id="ci-failed-b-polish",
    ),
    pytest.param(
        _pr(review_decision="CHANGES_REQUESTED"),
        EnumPRTrack.B_POLISH,
        EnumFailureCategory.CHANGES_REQUESTED,
        id="changes-requested-b-polish",
    ),
    pytest.param(
        # GitHub has not yet computed mergeability and there is no other blocker.
        _pr(mergeable="", merge_state_status="CLEAN"),
        EnumPRTrack.SKIP,
        None,
        id="no-actionable-state-skip",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(("pr", "expected_track", "expected_category"), _CASES)
async def test_merge_sweep_track_over_bus(
    integration_event_bus: Any,
    pr: ModelPRInfo,
    expected_track: EnumPRTrack,
    expected_category: EnumFailureCategory | None,
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _run_over_bus(bus, ModelMergeSweepRequest(prs=[pr]))
        assert len(result.classified) == 1
        classified = result.classified[0]
        assert classified.track == expected_track
        if expected_category is not None:
            assert expected_category.value in classified.failure_categories
        expected_status = (
            "nothing_to_merge" if expected_track == EnumPRTrack.SKIP else "queued"
        )
        assert result.status == expected_status
    finally:
        await bus.close()


@pytest.mark.integration
async def test_merge_sweep_negative_control_conflicting_not_merge_ready(
    integration_event_bus: Any,
) -> None:
    """A known-bad conflicting PR MUST land in Track B and MUST NOT be Track A."""
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _run_over_bus(
            bus, ModelMergeSweepRequest(prs=[_pr(mergeable="CONFLICTING")])
        )
        assert result.classified[0].track == EnumPRTrack.B_POLISH
        assert result.classified[0].track != EnumPRTrack.A_MERGE
        assert result.track_b_polish
        assert not result.track_a_merge
    finally:
        await bus.close()


@pytest.mark.integration
async def test_merge_sweep_require_approval_false_promotes_to_merge(
    integration_event_bus: Any,
) -> None:
    """With require_approval=False, an unapproved clean PR becomes merge-ready."""
    bus = integration_event_bus
    await bus.start()
    try:
        pr = _pr(review_decision=None, required_approving_review_count=5)
        result = await _run_over_bus(
            bus, ModelMergeSweepRequest(prs=[pr], require_approval=False)
        )
        assert result.classified[0].track == EnumPRTrack.A_MERGE
    finally:
        await bus.close()


@pytest.mark.integration
async def test_merge_sweep_max_total_merges_cap_over_bus(
    integration_event_bus: Any,
) -> None:
    """max_total_merges caps Track A; the overflow PR is re-tracked to skip."""
    bus = integration_event_bus
    await bus.start()
    try:
        prs = [_pr(number=n, review_decision="APPROVED") for n in (1, 2, 3)]
        result = await _run_over_bus(
            bus, ModelMergeSweepRequest(prs=prs, max_total_merges=2)
        )
        assert len(result.track_a_merge) == 2
        capped = [c for c in result.skipped if "max_total_merges" in c.reason]
        assert len(capped) == 1
    finally:
        await bus.close()


@pytest.mark.integration
async def test_merge_sweep_skip_polish_over_bus(
    integration_event_bus: Any,
) -> None:
    """skip_polish re-tracks Track B PRs to skip with the documented reason."""
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _run_over_bus(
            bus,
            ModelMergeSweepRequest(
                prs=[_pr(mergeable="CONFLICTING")], skip_polish=True
            ),
        )
        assert result.classified[0].track == EnumPRTrack.SKIP
        assert "Polish skipped" in result.classified[0].reason
    finally:
        await bus.close()


@pytest.mark.integration
async def test_merge_sweep_lifecycle_ordering_over_bus(
    integration_event_bus: Any,
) -> None:
    """use_lifecycle_ordering reorders Track A via the nested lifecycle pipeline;
    both Track A PRs survive the reorder and stay Track A."""
    bus = integration_event_bus
    await bus.start()
    try:
        prs = [_pr(number=n, review_decision="APPROVED") for n in (11, 12)]
        result = await _run_over_bus(
            bus, ModelMergeSweepRequest(prs=prs, use_lifecycle_ordering=True)
        )
        merged_numbers = {c.pr.number for c in result.track_a_merge}
        assert merged_numbers == {11, 12}
        assert result.status == "queued"
    finally:
        await bus.close()


@pytest.mark.integration
async def test_merge_sweep_chronic_skips_polish_over_bus(
    integration_event_bus: Any,
) -> None:
    """A CHRONIC failure history (>= 5 consecutive) re-tracks a Track B PR to skip."""
    bus = integration_event_bus
    await bus.start()
    try:
        pr = _pr(mergeable="CONFLICTING")
        key = f"{pr.repo}#{pr.number}"
        history = {
            key: ModelFailureHistoryEntry(
                first_seen="2026-07-01T00:00:00Z",
                last_seen="2026-07-02T00:00:00Z",
                consecutive_failures=5,
            )
        }
        result = await _run_over_bus(
            bus, ModelMergeSweepRequest(prs=[pr], failure_history=history)
        )
        assert result.classified[0].track == EnumPRTrack.SKIP
        assert "CHRONIC" in result.classified[0].reason
    finally:
        await bus.close()


@pytest.mark.integration
async def test_merge_sweep_recidivist_skips_polish_over_bus(
    integration_event_bus: Any,
) -> None:
    """A RECIDIVIST history (>= 3 polishes, still failing) re-tracks to skip."""
    bus = integration_event_bus
    await bus.start()
    try:
        pr = _pr(mergeable="CONFLICTING")
        key = f"{pr.repo}#{pr.number}"
        history = {
            key: ModelFailureHistoryEntry(
                first_seen="2026-07-01T00:00:00Z",
                last_seen="2026-07-02T00:00:00Z",
                consecutive_failures=1,
                total_polishes=3,
            )
        }
        result = await _run_over_bus(
            bus, ModelMergeSweepRequest(prs=[pr], failure_history=history)
        )
        assert result.classified[0].track == EnumPRTrack.SKIP
        assert "RECIDIVIST" in result.classified[0].reason
    finally:
        await bus.close()


@pytest.mark.integration
async def test_merge_sweep_failure_summary_ladder_over_bus(
    integration_event_bus: Any,
) -> None:
    """The failure-history summary counts STUCK, CHRONIC and RECIDIVIST buckets."""
    bus = integration_event_bus
    await bus.start()
    try:
        history = {
            "OmniNode-ai/omnimarket#1": ModelFailureHistoryEntry(
                first_seen="2026-07-01T00:00:00Z",
                last_seen="2026-07-02T00:00:00Z",
                consecutive_failures=3,  # STUCK
            ),
            "OmniNode-ai/omnimarket#2": ModelFailureHistoryEntry(
                first_seen="2026-07-01T00:00:00Z",
                last_seen="2026-07-02T00:00:00Z",
                consecutive_failures=5,  # CHRONIC
            ),
            "OmniNode-ai/omnimarket#3": ModelFailureHistoryEntry(
                first_seen="2026-07-01T00:00:00Z",
                last_seen="2026-07-02T00:00:00Z",
                consecutive_failures=1,
                total_polishes=3,  # RECIDIVIST
            ),
        }
        result = await _run_over_bus(
            bus, ModelMergeSweepRequest(prs=[], failure_history=history)
        )
        summary = result.failure_history_summary
        assert summary.total_tracked == 3
        assert summary.stuck_prs == 1
        assert summary.chronic_prs == 1
        assert summary.recidivist_prs == 1
        # Empty PR set with no actionable classification stays nothing_to_merge.
        assert result.status == "nothing_to_merge"
    finally:
        await bus.close()


@pytest.mark.integration
async def test_merge_sweep_pure_handler_matches_bus_result(
    integration_event_bus: Any,
) -> None:
    """The in-process pure return equals the bus-transited terminal payload,
    proving the runtime adapter does not mutate the COMPUTE result."""
    pr = _pr(review_decision="APPROVED")
    request = ModelMergeSweepRequest(prs=[pr])
    direct = NodeMergeSweep().handle(request)
    assert direct.classified[0].track == EnumPRTrack.A_MERGE
    assert direct.status == "queued"

    bus = integration_event_bus
    await bus.start()
    try:
        transited = await _run_over_bus(bus, request)
        assert transited.model_dump() == direct.model_dump()
    finally:
        await bus.close()
