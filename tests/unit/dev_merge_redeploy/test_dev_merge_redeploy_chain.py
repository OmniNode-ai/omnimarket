# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the lab dev-merge redeploy trigger (OMN-22478, R2).

Golden: publish the merge, assert every event the node produces, in order, under one
correlation id, ending in the verified receipt. Error: inject each defined fault and assert the
typed receipt; silence fails every one of them. A named injected break (a verify step that
always passes) is shown to turn the lane-behind test red.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers import (
    correlation_id_for,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.models import (
    EnumDevMergeOutcome,
    ModelDevMergeRedeployReceipt,
)
from omnimarket.nodes.node_redeploy_orchestrator.models.model_redeploy_start_command import (
    ModelRedeployStartCommand,
)

from .support import (
    BUILD_CONTEXT_SHA,
    LANE_SHA_AT_MERGE,
    LANE_SHA_OLDER,
    MERGE_SHA,
    REPO,
    FakeBuildContext,
    FakeClassifier,
    FakeFiles,
    FakeLane,
    Rig,
    merged,
    wait_for,
)

pytestmark = pytest.mark.unit

CID = correlation_id_for(REPO, MERGE_SHA)


def test_golden_chain_merge_to_start_command_to_verified_receipt() -> None:
    async def scenario() -> None:
        async with Rig() as rig:
            await rig.publish_merge(merged())
            await wait_for(lambda: len(rig.commands()) == 1)
            assert rig.receipts() == []  # nothing is claimed before the lane is read
            await rig.publish_completed(CID)
            await wait_for(lambda: len(rig.receipts()) == 1)

            assert rig.topics_in_order() == [
                rig.topics.start_command,
                rig.topics.receipt,
            ]
            command = ModelRedeployStartCommand.model_validate(rig.commands()[0])
            assert command.correlation_id == CID
            assert command.git_ref == BUILD_CONTEXT_SHA
            assert command.runtime_lane.value == "dev"
            assert command.build_source.value == "workspace"
            assert command.requested_by == "lab-node/omnimarket/pr-7"
            assert command.requested_at is not None
            receipt = ModelDevMergeRedeployReceipt.model_validate(rig.receipts()[0])
            assert receipt.correlation_id == CID
            assert receipt.outcome is EnumDevMergeOutcome.VERIFIED
            assert receipt.command_published is True
            assert receipt.runtime_paths == 1
            assert receipt.lane_revision == LANE_SHA_AT_MERGE
            assert receipt.final_phase == "done"
            assert rig.host.pending == 0

    asyncio.run(scenario())


def test_golden_chain_a_merge_no_runtime_path_reaches_ends_in_a_declined_receipt() -> (
    None
):
    async def scenario() -> None:
        async with Rig(files=FakeFiles(("docs/readme.md",))) as rig:
            await rig.publish_merge(merged())
            await wait_for(lambda: len(rig.receipts()) == 1)
            assert rig.commands() == []
            receipt = ModelDevMergeRedeployReceipt.model_validate(rig.receipts()[0])
            assert receipt.outcome is EnumDevMergeOutcome.DECLINED_NOT_RUNTIME
            assert receipt.succeeded
            assert receipt.command_published is False

    asyncio.run(scenario())


def test_a_replayed_merge_event_publishes_one_command_and_one_receipt() -> None:
    async def scenario() -> None:
        async with Rig() as rig:
            await rig.publish_merge(merged())
            await rig.publish_merge(merged())
            await wait_for(lambda: rig.host.duplicates == 1)
            await wait_for(lambda: len(rig.commands()) == 1)
            await rig.publish_completed(CID)
            await wait_for(lambda: len(rig.receipts()) == 1)
            await asyncio.sleep(0.1)
            assert len(rig.commands()) == 1
            assert len(rig.receipts()) == 1

    asyncio.run(scenario())


def test_consumer_groups_share_the_merge_topic_and_split_the_completion_topic_per_host() -> (
    None
):
    async def scenario() -> None:
        async with Rig() as rig:
            groups = dict(rig.bus.groups)
            assert "node_dev_merge_redeploy_trigger_effect" in groups[rig.topics.merged]
            assert "h-test" not in groups[rig.topics.merged]
            assert groups[rig.topics.completed].endswith(".completed.h-test")

    asyncio.run(scenario())


ERROR_CASES: list[tuple[str, dict[str, Any], EnumDevMergeOutcome, bool]] = [
    (
        "files unreadable",
        {"files": FakeFiles(fail=True)},
        EnumDevMergeOutcome.FAILED_FILES_UNREADABLE,
        False,
    ),
    (
        "classifier unavailable",
        {"classifier": FakeClassifier(fail=True)},
        EnumDevMergeOutcome.FAILED_CLASSIFIER,
        False,
    ),
    (
        "build-context ref unreadable",
        {"build_context": FakeBuildContext(fail=True)},
        EnumDevMergeOutcome.FAILED_BUILD_CONTEXT_REF,
        False,
    ),
]


@pytest.mark.parametrize(("name", "kwargs", "outcome", "published"), ERROR_CASES)
def test_error_chain_a_failed_read_is_a_typed_receipt_and_publishes_no_command(
    name: str, kwargs: dict[str, Any], outcome: EnumDevMergeOutcome, published: bool
) -> None:
    async def scenario() -> None:
        async with Rig(**kwargs) as rig:
            await rig.publish_merge(merged())
            await wait_for(lambda: len(rig.receipts()) == 1)
            receipt = ModelDevMergeRedeployReceipt.model_validate(rig.receipts()[0])
            assert receipt.outcome is outcome, name
            assert not receipt.succeeded
            assert rig.commands() == [], "a failed read must never publish a rebuild"
            assert receipt.message, "a failure names its cause"

    asyncio.run(scenario())


def test_error_chain_a_wrong_base_and_an_unknown_repository_are_declined_receipts() -> (
    None
):
    async def scenario() -> None:
        async with Rig() as rig:
            await rig.publish_merge(merged(base="main"))
            await rig.publish_merge(merged(repo="OmniNode-ai/other", sha="b" * 40))
            await wait_for(lambda: len(rig.receipts()) == 2)
            outcomes = sorted(r["outcome"] for r in rig.receipts())
            assert outcomes == ["declined_base", "declined_repo"]
            assert rig.commands() == []
            assert rig.files.calls == 0

    asyncio.run(scenario())


def test_error_chain_the_broker_refusing_the_command_is_a_failed_publish_receipt() -> (
    None
):
    async def scenario() -> None:
        async with Rig() as rig:
            rig.bus.fail_topic = rig.topics.start_command
            await rig.publish_merge(merged())
            await wait_for(lambda: len(rig.receipts()) == 1)
            receipt = ModelDevMergeRedeployReceipt.model_validate(rig.receipts()[0])
            assert receipt.outcome is EnumDevMergeOutcome.FAILED_PUBLISH
            assert receipt.command_published is False
            assert rig.host.pending == 0

    asyncio.run(scenario())


def test_error_chain_a_redeploy_that_did_not_finish_is_deploy_not_done_and_the_lane_is_not_read() -> (
    None
):
    async def scenario() -> None:
        lane = FakeLane()
        async with Rig(lane=lane) as rig:
            await rig.publish_merge(merged())
            await wait_for(lambda: len(rig.commands()) == 1)
            await rig.publish_completed(
                CID, final_phase="failed", error="health check failed"
            )
            await wait_for(lambda: len(rig.receipts()) == 1)
            receipt = ModelDevMergeRedeployReceipt.model_validate(rig.receipts()[0])
            assert receipt.outcome is EnumDevMergeOutcome.DEPLOY_NOT_DONE
            assert receipt.final_phase == "failed"
            assert "health check failed" in receipt.message
            assert lane.reads == 0

    asyncio.run(scenario())


def _lane_behind_scenario(make_rig: Callable[[], Rig]) -> Callable[[], Any]:
    async def scenario() -> ModelDevMergeRedeployReceipt:
        async with make_rig() as rig:
            await rig.publish_merge(merged())
            await wait_for(lambda: len(rig.commands()) == 1)
            await rig.publish_completed(CID)
            await wait_for(lambda: len(rig.receipts()) == 1)
            receipt = ModelDevMergeRedeployReceipt.model_validate(rig.receipts()[0])
            assert receipt.outcome is EnumDevMergeOutcome.LANE_BEHIND
            assert receipt.lane_revision == LANE_SHA_OLDER
            assert not receipt.succeeded
            return receipt

    return lambda: asyncio.run(scenario())


def _behind_rig() -> Rig:
    return Rig(lane=FakeLane(lane_sha=LANE_SHA_OLDER, carries=False))


def test_error_chain_a_lane_that_does_not_carry_the_merge_is_lane_behind() -> None:
    _lane_behind_scenario(_behind_rig)()


def test_injected_break_a_verify_that_always_passes_turns_the_lane_behind_test_red() -> (
    None
):
    """The failing control: with the lane read replaced by one that always carries the merge,
    the lane-behind scenario must fail. If it does not, the test above proves nothing."""

    class AlwaysCarries(FakeLane):
        def lane_carries(
            self, clone_dir: str, base: str, merge_sha: str, lane_sha: str
        ) -> bool:
            return True

    broken = _lane_behind_scenario(
        lambda: Rig(lane=AlwaysCarries(lane_sha=LANE_SHA_OLDER))
    )
    with pytest.raises(AssertionError):
        broken()


def test_error_chain_an_unreadable_lane_is_lane_unreadable() -> None:
    async def scenario() -> None:
        async with Rig(lane=FakeLane(unreadable=True)) as rig:
            await rig.publish_merge(merged())
            await wait_for(lambda: len(rig.commands()) == 1)
            await rig.publish_completed(CID)
            await wait_for(lambda: len(rig.receipts()) == 1)
            assert rig.receipts()[0]["outcome"] == "lane_unreadable"

    asyncio.run(scenario())


def test_error_chain_no_terminal_redeploy_event_by_the_deadline_is_a_deadline_missed_receipt() -> (
    None
):
    async def scenario() -> None:
        async with Rig(deadline=0.05) as rig:
            await rig.publish_merge(merged())
            await wait_for(lambda: len(rig.receipts()) == 1)
            receipt = ModelDevMergeRedeployReceipt.model_validate(rig.receipts()[0])
            assert receipt.outcome is EnumDevMergeOutcome.DEADLINE_MISSED
            assert receipt.command_published is True
            assert rig.host.pending == 0
            # A completion that arrives after the deadline matches nothing and makes no second receipt.
            await rig.publish_completed(CID)
            await wait_for(lambda: rig.host.unmatched_completions == 1)
            await asyncio.sleep(0.1)
            assert len(rig.receipts()) == 1

    asyncio.run(scenario())


def test_error_chain_unreadable_events_are_counted_and_make_no_receipt() -> None:
    async def scenario() -> None:
        async with Rig() as rig:
            await rig.bus.publish(rig.topics.merged, None, b"not json")
            await rig.publish_merge(merged().model_copy(update={"state": "closed"}))
            await rig.bus.publish(rig.topics.completed, None, b"{}")
            await wait_for(lambda: rig.host.skipped == 3)
            assert rig.receipts() == []
            assert rig.commands() == []

    asyncio.run(scenario())


def test_a_completion_for_another_correlation_id_is_not_ours() -> None:
    async def scenario() -> None:
        async with Rig() as rig:
            await rig.publish_merge(merged())
            await wait_for(lambda: len(rig.commands()) == 1)
            await rig.publish_completed("11111111-1111-1111-1111-111111111111")
            await wait_for(lambda: rig.host.unmatched_completions == 1)
            assert rig.receipts() == []
            assert rig.host.pending == 1

    asyncio.run(scenario())
