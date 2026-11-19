# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain event tests of the host local config drift read (OMN-24242).

Publish a broken command on the in-memory bus and assert the typed failure
event and the run-completed event that always follows it, never a dropped
message.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnimarket.nodes.node_host_local_config_drift_compute.models.model_host_local_config_drift import (
    ModelLocalConfigDriftCommand,
)
from tests.nodes.node_host_local_config_drift_compute.support import (
    declaration,
    measured_matching,
)
from tests.nodes.node_host_local_config_drift_compute.test_golden_chain_host_local_config_drift import (
    CORRELATION,
    TOPICS,
    publish,
)

pytestmark = pytest.mark.unit

BAD_PAYLOAD = b'{"correlation_id": "chain-1"}'


def mismatched_payload() -> bytes:
    """The command bytes of a measurement taken on a host its declaration does not name."""
    declared = declaration()
    measured = measured_matching(declared).model_copy(update={"host": "host-elsewhere"})
    return (
        ModelLocalConfigDriftCommand(
            correlation_id=CORRELATION,
            declaration=declared,
            measured=measured,
        )
        .model_dump_json()
        .encode()
    )


def assert_failed_chain(
    events: list[tuple[str, dict[str, Any]]],
    *,
    correlation_id: str,
    failure: str,
) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["host_local_config_drift_failed"],
        TOPICS["host_local_config_drift_run_completed"],
    ], topics
    assert events[0][1]["correlation_id"] == correlation_id
    assert events[0][1]["failure"] == failure
    assert events[-1][1]["correlation_id"] == correlation_id
    assert events[-1][1]["failed"] == 1
    assert events[-1][1]["findings"] == 0


def test_error_chain_a_bad_payload_is_answered_with_a_typed_failure() -> None:
    """A payload that is not a command is refused; its correlation id is unknown."""
    events = asyncio.run(publish(BAD_PAYLOAD, TOPICS))

    assert_failed_chain(
        events,
        correlation_id="unknown",
        failure="bad-payload:['declaration', 'measured']",
    )


def test_error_chain_a_measurement_of_another_host_is_a_typed_failure() -> None:
    """A measurement of another host is refused under the command's correlation id."""
    events = asyncio.run(publish(mismatched_payload(), TOPICS))

    assert_failed_chain(events, correlation_id=CORRELATION, failure="host-mismatch")
