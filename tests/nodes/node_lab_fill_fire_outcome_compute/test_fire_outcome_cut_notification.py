# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A cut completion notification is not a failed fire when the fire's own written result file is there (OMN-22431).

328 of 458 finished lab-fill fires in a week ended with the launcher agent replying WORKFLOW_RESULT_TRUNCATED, and the
tick receipted each of them degraded although the workflow had written its result file. The written file is the outcome.
The failing control is the same fire with no written file, which must still degrade.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_lab_fill_fire_outcome_compute.handlers.handler_lab_fill_fire_outcome import (
    HandlerLabFillFireOutcome,
)
from omnimarket.nodes.node_lab_fill_fire_outcome_compute.models import (
    EnumLabFillResultChannel,
    ModelLabFillFireOutcomePolicy,
)

from .conftest import make_request

pytestmark = pytest.mark.unit


def test_cut_notification_with_written_result_file_is_completed(
    policy: ModelLabFillFireOutcomePolicy,
) -> None:
    result = HandlerLabFillFireOutcome().handle(
        make_request(policy, truncated_marker_seen=True)
    )
    assert (result.phase, result.exit, result.degraded_class) == ("completed", 0, None)
    assert result.result_channel is EnumLabFillResultChannel.READ
    assert result.result_truncated is True
    assert result.result_file_is_outcome is True
    assert result.alert is None
    assert result.latest_value is not None


def test_cut_notification_with_no_result_file_stays_degraded(
    policy: ModelLabFillFireOutcomePolicy,
) -> None:
    """FAILING CONTROL: a cut completion with no written result file still degrades."""
    result = HandlerLabFillFireOutcome().handle(
        make_request(
            policy,
            truncated_marker_seen=True,
            file_state="absent",
            file_nonempty=False,
            file_text=None,
        )
    )
    assert (result.phase, result.exit, result.degraded_class) == (
        "degraded",
        70,
        "workflow-result-truncated",
    )
    assert result.result_truncated is True
    assert result.result_file_is_outcome is False
    assert result.result_channel is EnumLabFillResultChannel.ABSENT
    assert result.alert is None
    assert result.notice_line == (
        "lab-fill degraded: class=workflow-result-truncated; the Workflow result was cut before the agent read it"
        " and no summary was built (full result file: /state/results/20261113T060000Z-lab-fill.workflow-result.txt)"
    )


def test_cut_notification_with_empty_result_file_stays_degraded_then_invalid(
    policy: ModelLabFillFireOutcomePolicy,
) -> None:
    """An empty file is not an outcome: stage one degrades, stage two then reads it as invalid."""
    result = HandlerLabFillFireOutcome().handle(
        make_request(
            policy, truncated_marker_seen=True, file_nonempty=False, file_text=""
        )
    )
    assert (result.phase, result.exit, result.result_channel) == (
        "result_invalid",
        65,
        EnumLabFillResultChannel.INVALID,
    )
    assert result.degraded_class == "workflow-result-truncated"
    assert result.result_truncated is True


def test_cut_notification_with_invalid_result_file_is_result_invalid(
    policy: ModelLabFillFireOutcomePolicy,
) -> None:
    result = HandlerLabFillFireOutcome().handle(
        make_request(policy, truncated_marker_seen=True, file_text="not json")
    )
    assert (result.phase, result.exit) == ("result_invalid", 65)
    assert result.result_channel is EnumLabFillResultChannel.INVALID
    assert result.result_truncated is True
    assert result.result_file_is_outcome is True


def test_cut_notification_marker_ignored_when_the_run_did_not_complete(
    policy: ModelLabFillFireOutcomePolicy,
) -> None:
    result = HandlerLabFillFireOutcome().handle(
        make_request(policy, phase="failed", exit=1, truncated_marker_seen=True)
    )
    assert (result.phase, result.exit, result.result_truncated) == ("failed", 1, False)
    assert result.result_channel is EnumLabFillResultChannel.READ


def test_cut_notification_marker_ignored_without_a_result_channel(
    policy: ModelLabFillFireOutcomePolicy,
) -> None:
    result = HandlerLabFillFireOutcome().handle(
        make_request(policy, result_channel_enabled=False, truncated_marker_seen=True)
    )
    assert result.result_truncated is False
    assert result.phase == "completed"


@pytest.mark.parametrize("nonempty", [True, False])
def test_cut_notification_verdict_turns_on_the_written_file_alone(
    policy: ModelLabFillFireOutcomePolicy, nonempty: bool
) -> None:
    """Injected break: flipping only the written file's presence flips completed to degraded."""
    result = HandlerLabFillFireOutcome().handle(
        make_request(
            policy,
            truncated_marker_seen=True,
            file_nonempty=nonempty,
            file_state="present" if nonempty else "absent",
            file_text=make_request(policy).file_text if nonempty else None,
        )
    )
    assert (result.phase == "completed") is nonempty
