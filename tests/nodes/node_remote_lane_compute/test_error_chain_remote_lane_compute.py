# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: undecidable requests are refused, and no lane closes done by accident (OMN-20669)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_remote_lane_close_compute.handlers import (
    HandlerRemoteLaneResult,
)
from omnimarket.nodes.node_remote_lane_close_compute.models import (
    ModelRemoteLaneResultRequest,
)
from omnimarket.nodes.node_remote_lane_compute.handlers import (
    HandlerRemoteLanePlacement,
)
from omnimarket.nodes.node_remote_lane_compute.models import (
    ModelRemoteLanePlacementRequest,
)


@pytest.mark.parametrize(
    "request_fields",
    [
        {"engine": ""},
        {"engine": "claude_opus", "readings": [{"name": ""}]},
        {"engine": "claude_opus", "readings": [{"name": "h", "placed": -1}]},
        {"engine": "claude_opus", "readings": [{"name": "h", "host_os": "linux"}]},
        {"engine": "claude_opus", "unknown_field": 1},
    ],
)
def test_malformed_placement_requests_are_refused(
    request_fields: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ModelRemoteLanePlacementRequest.model_validate(request_fields)


def test_no_host_is_named_when_none_qualifies() -> None:
    result = HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest(
            engine="claude_sonnet",
            pinned_host="host_c",
            readings=(
                {"name": "host_a", "engines": ("claude_sonnet",), "lane_slots": 0},
                {
                    "name": "host_b",
                    "lane_admission_refusal": "LOAD-OVER-BAR",
                    "lane_slots": 3,
                },
                {"name": "host_c", "engines": ("claude_opus",), "lane_slots": 3},
            ),
        )
    )
    assert result.host is None
    assert [v.reason for v in result.verdicts] == [
        "not-pinned",
        "admission: LOAD-OVER-BAR",
        "no-claude_sonnet-login",
    ]


def test_an_empty_pool_places_nothing() -> None:
    result = HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest(engine="codex")
    )
    assert (result.host, result.verdicts) == (None, ())


@pytest.mark.parametrize(
    ("code", "message", "outcome"),
    [
        (1, "LANE_RESULT outcome=done\nDELEGATION delegated=1 runs=r", "failed"),
        (0, None, "rejected-no-delegation"),
        (0, "LANE_RESULT outcome=done", "rejected-no-delegation"),
        (
            0,
            "LANE_RESULT outcome=done\nDELEGATION delegated=2",
            "rejected-no-delegation",
        ),
        (
            0,
            "LANE_RESULT outcome=done\nDELEGATION delegated=0 reason=busy",
            "rejected-no-delegation",
        ),
        (0, "all good\nDELEGATION delegated=1 runs=r", "unknown"),
        (
            0,
            "```\nLANE_RESULT outcome=done\n```\nDELEGATION delegated=1 runs=r",
            "unknown",
        ),
    ],
)
def test_a_lane_without_proof_never_closes_done(
    code: int, message: str | None, outcome: str
) -> None:
    result = HandlerRemoteLaneResult().handle(
        ModelRemoteLaneResultRequest(engine_exit_code=code, final_message=message)
    )
    assert result.outcome == outcome


def test_an_undeclared_reason_is_named_with_the_declared_set() -> None:
    result = HandlerRemoteLaneResult().handle(
        ModelRemoteLaneResultRequest(
            engine_exit_code=0,
            final_message="DELEGATION delegated=0 reason=busy",
            delegation_reasons=("no-text-or-code", "read-only-lane"),
        )
    )
    assert result.problem == (
        "DELEGATION delegated=0 reason=busy is not one of no-text-or-code, read-only-lane"
    )


@pytest.mark.parametrize(
    ("message", "waiting"),
    [
        ("I\u2019ll be notified when CI finishes.", True),
        ("Waiting on CI.", True),
        ("Waiting on CI. Handed omnimarket#12 msg=abc", False),
        ("LANE_RESULT outcome=done", False),
    ],
)
def test_a_lane_waiting_on_a_notification_is_named(message: str, waiting: bool) -> None:
    result = HandlerRemoteLaneResult().handle(
        ModelRemoteLaneResultRequest(engine_exit_code=0, final_message=message)
    )
    assert result.waiting_on_background is waiting
