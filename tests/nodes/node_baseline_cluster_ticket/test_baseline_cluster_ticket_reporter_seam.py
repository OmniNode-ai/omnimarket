# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the dev-baseline reporter and the cluster ticket node (OMN-19998).

The reporter (omnibase_infra scripts/publish_test_failure_baseline.py) runs ``onex run-node node_baseline_cluster_ticket_effect
--input <json>`` and reads ``result.state`` and ``result.ticket`` from the one JSON object the node prints. This test pins both
sides as the reporter's own test pins them (tests/ci/test_test_failure_ratchet.py): the key set of the input and the two keys
of the answer. It fails when the node's command gains or loses a field the reporter sends, and when the node's answer loses a
key the reporter reads.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_baseline_cluster_ticket_effect.models import (
    ModelBaselineClusterReportCommand,
    ModelBaselineClusterReportCompleted,
    ModelBaselineClusterReportResult,
)

pytestmark = pytest.mark.unit

# The input the reporter sends: exactly these keys (run-node adds correlation_id from the command envelope).
REPORTER_INPUT = {
    "requested_by_lane": "dev-baseline-publisher",
    "repo": "omnibase_infra",
    "cluster": "tests.unit.test_foo",
    "failing_count": 7,
    "examples": ["tests.unit.test_foo::test_a"],
    "run_url": "OmniNode-ai/omnibase_infra Actions run 424242",
    "observed_at": "2026-11-15T03:00:00+00:00",
}
# What the reporter reads of the answer: result.state and result.ticket.
REPORTER_READS = ("state", "ticket")


def test_the_reporters_input_is_exactly_the_command_the_node_takes() -> None:
    command_fields = set(ModelBaselineClusterReportCommand.model_fields) - {
        "correlation_id"
    }
    assert set(REPORTER_INPUT) == command_fields
    command = ModelBaselineClusterReportCommand.model_validate(
        {**REPORTER_INPUT, "correlation_id": str(uuid4())}
    )
    assert command.failing_count == 7


def test_the_node_answer_carries_the_two_keys_the_reporter_reads() -> None:
    completed = ModelBaselineClusterReportCompleted(
        correlation_id=uuid4(),
        requested_by_lane="dev-baseline-publisher",
        repo="omnibase_infra",
        cluster="tests.unit.test_foo",
        result=ModelBaselineClusterReportResult(state="commented", ticket="OMN-90001"),
    )
    answer = completed.model_dump(mode="json")
    assert set(REPORTER_READS) <= set(answer["result"])
    assert answer["result"]["state"] == "commented"
    assert answer["result"]["ticket"] == "OMN-90001"


def test_control_a_command_field_the_reporter_does_not_send_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelBaselineClusterReportCommand.model_validate(
            {**REPORTER_INPUT, "correlation_id": str(uuid4()), "priority": 1}
        )


def test_control_an_input_missing_a_field_the_node_requires_is_refused() -> None:
    for key in REPORTER_INPUT:
        partial = {k: v for k, v in REPORTER_INPUT.items() if k != key}
        with pytest.raises(ValidationError):
            ModelBaselineClusterReportCommand.model_validate(
                {**partial, "correlation_id": str(uuid4())}
            )
