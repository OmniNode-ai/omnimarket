# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Incomplete captures and conflicting lane metadata must not invent signals."""

import pytest

from omnimarket.nodes.node_delegation_hook_followup_compute.handlers.handler_delegation_hook_followup import (
    HandlerDelegationHookFollowup,
)
from omnimarket.nodes.node_delegation_hook_followup_compute.models.model_hook_followup_request import (
    ModelHookEvent,
    ModelHookFollowupRequest,
    ModelHookLaneRow,
    ModelHookReceipt,
)

pytestmark = pytest.mark.unit
_DIGEST = "a" * 64
_PR = "omnimarket#123"


def _event(ts: int, **changes: object) -> ModelHookEvent:
    fields: dict[str, object] = {
        "timestamp_ms": ts,
        "session_id": "session",
        "agent_id": "agent",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_use_id": "delegate",
        "tool_input_sha256": _DIGEST,
    }
    fields.update(changes)
    return ModelHookEvent.model_validate(fields)


def _row(ts: int, **changes: object) -> ModelHookLaneRow:
    fields: dict[str, object] = {
        "timestamp_ms": ts,
        "kind": "TERMINAL",
        "lane": "lane",
        "pr": _PR,
    }
    fields.update(changes)
    return ModelHookLaneRow.model_validate(fields)


def _request(
    events: tuple[ModelHookEvent, ...],
    rows: tuple[ModelHookLaneRow, ...] = (),
    *,
    start: int = 0,
    end: int = 1000,
    receipt_pr: str | None = _PR,
) -> ModelHookFollowupRequest:
    return ModelHookFollowupRequest(
        receipts=(
            ModelHookReceipt(
                receipt_key="answer",
                lane="lane",
                started_at_ms=100,
                ended_at_ms=200,
                command_sha256=_DIGEST,
                hook_session_id="session",
                hook_agent_id="agent",
                pr=receipt_pr,
            ),
        ),
        hook_events=events,
        lane_rows=rows,
        window_start_ms=start,
        window_end_ms=end,
    )


@pytest.mark.parametrize(
    ("events", "rows", "start", "end", "join"),
    [
        ((), (_row(500),), 0, 1000, "none"),
        ((_event(100),), (), 0, 1000, "digest"),
        ((_event(100),), (_row(199), _row(500, lane="peer")), 0, 1000, "digest"),
        ((_event(150),), (_row(500),), 101, 1000, "digest"),
        ((_event(100),), (_row(500),), 0, 499, "digest"),
        (
            (_event(100),),
            (_row(500), _row(500, pr="omnimarket#999")),
            0,
            1000,
            "ambiguous",
        ),
    ],
    ids=[
        "no-identity",
        "no-terminal",
        "irrelevant-terminals",
        "missing-start",
        "missing-end",
        "conflicting-terminal-prs",
    ],
)
def test_no_signal_for_incomplete_or_ambiguous_input(
    events: tuple[ModelHookEvent, ...],
    rows: tuple[ModelHookLaneRow, ...],
    start: int,
    end: int,
    join: str,
) -> None:
    followup = (
        HandlerDelegationHookFollowup()
        .handle(_request(events, rows, start=start, end=end))
        .followups[0]
    )
    assert (followup.receipt_key, followup.join, followup.signal) == (
        "answer",
        join,
        None,
    )


@pytest.mark.parametrize("receipt_pr", [_PR, None])
@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, True),
        ({"timestamp_ms": 500}, False),
        ({"timestamp_ms": 1001}, False),
        ({"lane": "lane"}, False),
        ({"action": "review"}, False),
        ({"kind": "TERMINAL"}, False),
        ({"pr": "omnimarket#999"}, False),
    ],
    ids=[
        "matching-fix",
        "at-terminal",
        "after-capture",
        "same-lane",
        "other-action",
        "other-kind",
        "other-pr",
    ],
)
def test_peer_fix_requires_matching_claim_after_terminal_in_capture(
    receipt_pr: str | None,
    changes: dict[str, object],
    expected: bool,
) -> None:
    fields: dict[str, object] = {
        "timestamp_ms": 600,
        "kind": "CLAIM",
        "lane": "peer",
        "action": "fix",
        "pr": _PR,
    }
    fields.update(changes)
    signal = (
        HandlerDelegationHookFollowup()
        .handle(
            _request(
                (_event(100),),
                (_row(500), ModelHookLaneRow.model_validate(fields)),
                receipt_pr=receipt_pr,
            )
        )
        .followups[0]
        .signal
    )
    assert signal is not None
    assert signal.peer_lane_returned_for_fix is expected
    assert signal.tool_calls == 0
    assert (signal.window_start_ms, signal.window_end_ms) == (200, 500)


def test_orphan_completions_and_missing_call_ids_preserve_observations() -> None:
    events = (
        _event(100),
        _event(300, hook_event_name="PostToolUseFailure", tool_use_id="orphan"),
        _event(310, tool_use_id="unknown", tool_name=None),
        _event(
            320, hook_event_name="PostToolUse", tool_use_id="unknown", tool_name="Write"
        ),
        _event(330, tool_use_id=None),
        _event(340, hook_event_name="Stop", tool_use_id="ignored"),
        _event(1100, tool_use_id="outside", tool_name="Edit"),
    )
    signal = (
        HandlerDelegationHookFollowup()
        .handle(_request(events, (_row(500),)))
        .followups[0]
        .signal
    )
    assert signal is not None
    assert (
        signal.tool_calls,
        signal.failures,
        signal.retries,
        signal.write_count,
        signal.edit_count,
    ) == (2, 1, 0, 1, 0)
    assert signal.repeated_digests == ()


@pytest.mark.parametrize("agent_count", [4, 5])
def test_common_digests_stop_counting_as_repeated_work(agent_count: int) -> None:
    events = [_event(100)]
    for index in range(agent_count):
        agent = "agent" if index == 0 else f"peer-{index}"
        for digest_index in range(5):
            changes: dict[str, object] = {
                "agent_id": agent,
                "tool_use_id": f"work-{digest_index}",
                "tool_input_sha256": f"{digest_index:064x}",
            }
            events.extend(
                (
                    _event(300 + digest_index * 10, **changes),
                    _event(
                        301 + digest_index * 10,
                        hook_event_name="PostToolUse",
                        **changes,
                    ),
                )
            )
    signal = (
        HandlerDelegationHookFollowup()
        .handle(_request(tuple(events), (_row(500),)))
        .followups[0]
        .signal
    )
    assert signal is not None
    assert signal.repeated_work_agents == (
        tuple(f"session/peer-{i}" for i in range(1, agent_count))
        if agent_count == 4
        else ()
    )
