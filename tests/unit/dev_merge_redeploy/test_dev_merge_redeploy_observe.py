# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Observe mode: the node publishes no command and reads the lane until it carries the merge.

Observe mode is how the node runs beside the Actions trigger before the cutover: the trigger
still redeploys, and the node proves it can read a real dev merge, classify it, read the lane
and publish its receipt. The same suite runs the failure shapes, each with its own receipt.
"""

from __future__ import annotations

import asyncio

import pytest

from omnimarket.models.dev_merge_redeploy_overlay import (
    EnumDevMergeMode,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.handler_dev_merge_redeploy_verify import (
    OBSERVED_PHASE,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.models import (
    EnumDevMergeOutcome,
    ModelDevMergeRedeployReceipt,
)

from .support import (
    LANE_SHA_AT_MERGE,
    LANE_SHA_OLDER,
    FakeClassifier,
    FakeFiles,
    FakeLane,
    Rig,
    merged,
    wait_for,
)

pytestmark = pytest.mark.unit


def _receipt(rig: Rig) -> ModelDevMergeRedeployReceipt:
    return ModelDevMergeRedeployReceipt.model_validate(rig.receipts()[0])


def test_golden_chain_observe_reads_the_lane_after_a_late_redeploy_and_publishes_no_command() -> (
    None
):
    async def scenario() -> None:
        # The lane is behind for the first two reads, as it is while the other trigger redeploys.
        async with Rig(mode="observe", lane=FakeLane(behind_reads=2)) as rig:
            await rig.publish_merge(merged())
            await wait_for(lambda: len(rig.receipts()) == 1)
            receipt = _receipt(rig)
            assert receipt.mode is EnumDevMergeMode.OBSERVE
            assert receipt.outcome is EnumDevMergeOutcome.VERIFIED
            assert receipt.command_published is False
            assert receipt.final_phase == OBSERVED_PHASE
            assert receipt.lane_revision == LANE_SHA_AT_MERGE
            assert rig.commands() == []
            assert rig.lane.reads >= 3

    asyncio.run(scenario())


def test_observe_does_not_listen_for_the_terminal_event() -> None:
    async def scenario() -> None:
        async with Rig(mode="observe") as rig:
            assert [t for t, _ in rig.bus.groups if t == rig.topics.completed] == []

    asyncio.run(scenario())


def test_observe_a_merge_no_runtime_path_reaches_reads_no_lane() -> None:
    async def scenario() -> None:
        lane = FakeLane()
        async with Rig(
            mode="observe", files=FakeFiles(("docs/readme.md",)), lane=lane
        ) as rig:
            await rig.publish_merge(merged())
            await wait_for(lambda: len(rig.receipts()) == 1)
            receipt = _receipt(rig)
            assert receipt.outcome is EnumDevMergeOutcome.DECLINED_NOT_RUNTIME
            assert receipt.mode is EnumDevMergeMode.OBSERVE
            assert lane.reads == 0

    asyncio.run(scenario())


def test_error_chain_observe_a_lane_that_never_carries_the_merge_is_a_deadline_missed_receipt() -> (
    None
):
    async def scenario() -> None:
        lane = FakeLane(lane_sha=LANE_SHA_OLDER, carries=False)
        async with Rig(mode="observe", lane=lane, deadline=0.2) as rig:
            await rig.publish_merge(merged())
            await wait_for(lambda: len(rig.receipts()) == 1)
            receipt = _receipt(rig)
            assert receipt.outcome is EnumDevMergeOutcome.DEADLINE_MISSED
            assert receipt.lane_revision == LANE_SHA_OLDER
            assert "does not carry" in receipt.message
            assert not receipt.succeeded
            assert rig.commands() == []

    asyncio.run(scenario())


def test_error_chain_observe_an_unreadable_lane_is_a_deadline_missed_receipt_naming_the_read() -> (
    None
):
    async def scenario() -> None:
        async with Rig(
            mode="observe", lane=FakeLane(unreadable=True), deadline=0.2
        ) as rig:
            await rig.publish_merge(merged())
            await wait_for(lambda: len(rig.receipts()) == 1)
            receipt = _receipt(rig)
            assert receipt.outcome is EnumDevMergeOutcome.DEADLINE_MISSED
            assert "LaneUnreadableError" in receipt.message

    asyncio.run(scenario())


def test_error_chain_observe_a_failed_classifier_is_a_typed_receipt_and_reads_no_lane() -> (
    None
):
    async def scenario() -> None:
        lane = FakeLane()
        async with Rig(
            mode="observe", classifier=FakeClassifier(fail=True), lane=lane
        ) as rig:
            await rig.publish_merge(merged())
            await wait_for(lambda: len(rig.receipts()) == 1)
            assert _receipt(rig).outcome is EnumDevMergeOutcome.FAILED_CLASSIFIER
            assert lane.reads == 0

    asyncio.run(scenario())


def test_injected_break_an_observe_that_always_verifies_turns_the_never_carries_test_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The named break: observe reports VERIFIED whatever the lane carries. The test must fail."""
    from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers import (
        handler_dev_merge_redeploy_verify as module,
    )
    from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.models.model_dev_merge_decision import (
        ModelDevMergeVerdict,
    )

    def always_verified(self: object, pending: object) -> ModelDevMergeVerdict:
        return ModelDevMergeVerdict(
            outcome=EnumDevMergeOutcome.VERIFIED, final_phase=OBSERVED_PHASE
        )

    monkeypatch.setattr(
        module.HandlerDevMergeRedeployVerify, "observe", always_verified
    )
    with pytest.raises(AssertionError):
        test_error_chain_observe_a_lane_that_never_carries_the_merge_is_a_deadline_missed_receipt()
