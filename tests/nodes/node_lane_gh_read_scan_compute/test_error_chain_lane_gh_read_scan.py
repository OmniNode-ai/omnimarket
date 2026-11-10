# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain event tests of the gh-read scan (OMN-18946): a bad command payload is answered with the typed failure event
on the contract's failure topic, under the command's correlation id when it has one, never with silence."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.nodes.node_lane_gh_read_scan_compute.test_golden_chain_lane_gh_read_scan import (
    FAILED,
    publish,
)

pytestmark = pytest.mark.unit


def test_error_chain_a_bad_payload_is_a_typed_failure_event() -> None:
    events = asyncio.run(
        publish(json.dumps({"correlation_id": "bad-1", "surfaces": "nope"}).encode())
    )
    assert [topic for topic, _ in events] == [FAILED]
    assert events[0][1]["correlation_id"] == "bad-1"
    assert events[0][1]["failure"] == "bad-payload"


def test_error_chain_a_payload_that_is_not_json_is_a_typed_failure_event() -> None:
    events = asyncio.run(publish(b"not json"))
    assert [topic for topic, _ in events] == [FAILED]
    assert events[0][1] == {
        "correlation_id": "",
        "failure": "bad-payload",
        "detail": events[0][1]["detail"],
    }
