# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain event tests of the overlay merge order check (OMN-22809).

Inject a defined fault and assert the typed failure event; nothing is passed when the check cannot be judged.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from omnimarket.enums.enum_overlay_verdict import EnumOverlayVerdict
from tests.unit.nodes.node_overlay_merge_order_compute.merge_order_support import (
    CORRELATION,
    TOPICS,
    check,
    command,
    handler,
    whole_home,
)
from tests.unit.nodes.node_overlay_merge_order_compute.test_golden_chain_overlay_merge_order import (
    payload_of,
    publish,
)

pytestmark = pytest.mark.unit


def assert_failed_chain(
    events: list[tuple[str, dict[str, Any]]],
    failure: str,
    correlation: str = CORRELATION,
) -> None:
    assert [topic for topic, _ in events] == [TOPICS["overlay_merge_order_run_failed"]]
    assert events[0][1] == {"correlation_id": correlation, "failure": failure}


def test_error_chain_a_change_with_no_overlay_is_refused() -> None:
    events = asyncio.run(publish(handler(), payload_of((), whole_home())))
    assert_failed_chain(events, "no_changed_overlays")


def test_error_chain_a_touched_overlay_the_merged_tree_never_judged_is_refused() -> (
    None
):
    events = asyncio.run(
        publish(handler(), payload_of(("node_zeta_effect",), whole_home()))
    )
    assert_failed_chain(events, "changed_overlay_not_judged:node_zeta_effect")


def test_error_chain_a_duplicated_answer_is_refused() -> None:
    events = asyncio.run(
        publish(
            handler(),
            payload_of(
                ("node_alpha_effect",), (*whole_home(), check("node_alpha_effect"))
            ),
        )
    )
    assert_failed_chain(events, "duplicate_check:node_alpha_effect")


def test_error_chain_a_verdict_the_overlay_does_not_rule_on_is_refused() -> None:
    events = asyncio.run(
        publish(
            handler(ahead_verdicts=["rejected"]),
            payload_of(("node_delta_effect",), whole_home()),
        )
    )
    assert_failed_chain(events, "verdict_not_ruled:node_delta_effect:model_missing")


def test_error_chain_a_payload_that_is_not_a_command_gets_a_typed_failure_not_silence() -> (
    None
):
    events = asyncio.run(publish(handler(), json.dumps({"surprise": 1}).encode()))
    assert [topic for topic, _ in events] == [TOPICS["overlay_merge_order_run_failed"]]
    assert events[0][1]["correlation_id"] == "unknown"
    assert events[0][1]["failure"].startswith("bad-payload:")


def test_error_chain_a_check_with_an_unknown_verdict_is_not_a_command() -> None:
    body = json.loads(command(("node_alpha_effect",), whole_home()).model_dump_json())
    body["merged_checks"][0]["verdict"] = "teapot"
    events = asyncio.run(publish(handler(), json.dumps(body).encode()))
    assert events[0][1]["failure"].startswith("bad-payload:")


def test_error_chain_failing_control_a_passing_decision_is_not_a_failure_chain() -> (
    None
):
    """The same assertion on a sound change must fail: the error chain asserts the refusal, not the data."""
    events = asyncio.run(
        publish(handler(), payload_of(("node_alpha_effect",), whole_home()))
    )
    with pytest.raises(AssertionError):
        assert_failed_chain(events, "no_changed_overlays")
    assert EnumOverlayVerdict.ACCEPTED.value == "accepted"
