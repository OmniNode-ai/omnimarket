# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Receipt-to-hook follow-up signals, with no correctness judgement."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_delegation_hook_followup_compute.handlers.handler_delegation_hook_followup import (
    HandlerDelegationHookFollowup,
    fold,
)
from omnimarket.nodes.node_delegation_hook_followup_compute.models.model_hook_followup_request import (
    ModelHookEvent,
    ModelHookFollowupRequest,
    ModelHookLaneRow,
    ModelHookReceipt,
)
from omnimarket.nodes.node_delegation_hook_followup_compute.models.model_hook_followup_result import (
    ModelHookFollowupResult,
)

DIGEST = "a" * 64
OTHER = "b" * 64


def event(
    ts: int,
    hook: str = "PreToolUse",
    *,
    agent: str | None = "agent-1",
    session: str = "session-1",
    tool: str = "Bash",
    call: str = "delegate",
    digest: str | None = DIGEST,
    cursor: int = 0,
) -> ModelHookEvent:
    return ModelHookEvent(
        cursor=cursor,
        timestamp_ms=ts,
        session_id=session,
        agent_id=agent,
        hook_event_name=hook,
        tool_name=tool,
        tool_use_id=call,
        tool_input_sha256=digest,
    )


def receipt(**changes: object) -> ModelHookReceipt:
    fields: dict[str, object] = {
        "receipt_key": "receipt-1",
        "lane": "lane-1",
        "started_at_ms": 100,
        "ended_at_ms": 200,
        "command_sha256": DIGEST,
        "answer_sha256": OTHER,
        "answer_ref": "lab/answer.txt",
        "hook_session_id": "session-1",
        "hook_agent_id": "agent-1",
        "pr": "omnimarket#123",
    }
    fields.update(changes)
    return ModelHookReceipt.model_validate(fields)


def request(
    events: tuple[ModelHookEvent, ...],
    item: ModelHookReceipt | None = None,
    rows: tuple[ModelHookLaneRow, ...] | None = None,
) -> ModelHookFollowupRequest:
    return ModelHookFollowupRequest(
        receipts=(item or receipt(),),
        hook_events=events,
        lane_rows=rows
        if rows is not None
        else (ModelHookLaneRow(timestamp_ms=1000, kind="TERMINAL", lane="lane-1"),),
        window_start_ms=0,
        window_end_ms=2000,
    )


def test_no_label_field() -> None:
    handler = HandlerDelegationHookFollowup()
    inputs = (
        request((event(100), event(300, tool="Edit", call="edit"))),
        request(
            (event(150), event(150, agent="agent-2")),
            receipt(command_sha256=None, hook_agent_id=None),
        ),
        request(()),
    )
    for req in inputs:
        result = handler.handle(req)
        assert "label" not in json.dumps(result.model_dump(mode="json"))
    assert "label" not in json.dumps(ModelHookFollowupResult.model_json_schema())
    with pytest.raises(ValidationError):
        ModelHookFollowupResult.model_validate({"followups": [], "label": "right"})


def test_ambiguous_returns_none() -> None:
    req = request(
        (event(150), event(160, agent="agent-2")),
        receipt(command_sha256=None, hook_agent_id=None),
    )
    followup = HandlerDelegationHookFollowup().handle(req).followups[0]
    assert followup.join == "ambiguous"
    assert followup.signal is None


def test_exact_digest_match_yields_join_digest() -> None:
    req = request(
        (event(100), event(200, "PostToolUse"), event(300, tool="Edit", call="edit"))
    )
    followup = HandlerDelegationHookFollowup().handle(req).followups[0]
    assert followup.receipt_key == "receipt-1"
    assert followup.join == "digest"
    assert followup.signal is not None
    assert followup.signal.tool_calls == 1
    assert followup.signal.edit_count == 1


def test_fold_failures_refusals_retries_and_repeated_digests() -> None:
    events = (
        event(100),
        event(200, "PostToolUse"),
        event(300, call="fail", digest=OTHER),
        event(310, "PostToolUseFailure", call="fail"),
        event(400, call="retry", digest=OTHER),
        event(410, "PermissionDenied", call="retry"),
        event(411, "PostToolUseFailure", call="retry"),
        event(500, call="retry-again", digest=OTHER),
        event(510, "PostToolUse", call="retry-again"),
        event(600, tool="Write", call="write", digest=None),
        event(610, "PostToolUse", tool="Write", call="write"),
    )
    signal = HandlerDelegationHookFollowup().handle(request(events)).followups[0].signal
    assert signal is not None
    assert (signal.tool_calls, signal.failures, signal.refusals, signal.retries) == (
        4,
        1,
        1,
        2,
    )
    assert signal.repeated_digests == (OTHER,)
    assert signal.write_count == 1


def test_time_fallback_uses_delegate_window_for_unique_agent() -> None:
    req = request(
        (event(150, digest=OTHER), event(300, tool="Write", call="write")),
        receipt(command_sha256=None, hook_agent_id=None),
    )
    followup = HandlerDelegationHookFollowup().handle(req).followups[0]
    assert followup.join == "time"
    assert followup.signal is not None
    assert followup.signal.write_count == 1


def test_digest_does_not_join_another_session_or_agent() -> None:
    req = request(
        (event(100, session="other-session"), event(100, agent="other-agent"))
    )
    followup = HandlerDelegationHookFollowup().handle(req).followups[0]
    assert followup.join == "none"
    assert followup.signal is None


def test_duplicate_digest_calls_in_delegate_window_are_ambiguous() -> None:
    req = request((event(110, call="one"), event(120, call="two")))
    followup = HandlerDelegationHookFollowup().handle(req).followups[0]
    assert followup.join == "ambiguous"
    assert followup.signal is None


def test_digest_outside_delegate_window_does_not_join() -> None:
    req = request(
        (event(50), event(150, digest=OTHER), event(300, tool="Edit", call="edit"))
    )
    followup = HandlerDelegationHookFollowup().handle(req).followups[0]
    assert followup.join == "time"


def test_next_terminal_bounds_signal_and_peer_fix_requires_matching_pr() -> None:
    rows = (
        ModelHookLaneRow(timestamp_ms=90, kind="TERMINAL", lane="lane-1"),
        ModelHookLaneRow(
            timestamp_ms=500, kind="TERMINAL", lane="lane-1", pr="omnimarket#123"
        ),
        ModelHookLaneRow(timestamp_ms=800, kind="TERMINAL", lane="lane-1"),
        ModelHookLaneRow(
            timestamp_ms=600,
            kind="CLAIM",
            lane="peer",
            action="fix",
            pr="omnimarket#123",
        ),
    )
    events = (
        event(100),
        event(300, tool="Edit", call="edit"),
        event(700, tool="Write", call="late"),
    )
    signal = (
        HandlerDelegationHookFollowup()
        .handle(request(events, rows=rows))
        .followups[0]
        .signal
    )
    assert signal is not None
    assert (
        signal.edit_count,
        signal.write_count,
        signal.peer_lane_returned_for_fix,
    ) == (1, 0, True)
    other_rows = (*rows[:3], rows[3].model_copy(update={"pr": "omnimarket#999"}))
    signal = (
        HandlerDelegationHookFollowup()
        .handle(request(events, rows=other_rows))
        .followups[0]
        .signal
    )
    assert signal is not None
    assert signal.peer_lane_returned_for_fix is False


def test_missing_terminal_or_incomplete_capture_returns_no_signal() -> None:
    handler = HandlerDelegationHookFollowup()
    req = request((event(100),), rows=())
    assert handler.handle(req).followups[0].signal is None
    req = request((event(100),)).model_copy(update={"window_end_ms": 500})
    assert handler.handle(req).followups[0].signal is None


def test_order_independent_and_stateless() -> None:
    events = (
        event(100),
        event(300, call="retry"),
        event(310, "PermissionDenied", call="retry"),
        event(311, "PostToolUseFailure", call="retry"),
    )
    handler = HandlerDelegationHookFollowup()
    expected = handler.handle(request(events))
    assert handler.handle(request(tuple(reversed(events)))) == expected
    handler.handle(request(()))
    assert handler.handle(request(events)) == expected


def test_every_receipt_has_a_result() -> None:
    req = request((event(100),)).model_copy(
        update={
            "receipts": (
                receipt(),
                receipt(receipt_key="second", hook_agent_id="missing"),
            )
        }
    )
    result = HandlerDelegationHookFollowup().handle(req)
    assert [item.receipt_key for item in result.followups] == ["receipt-1", "second"]
    assert result.followups[1].signal is None


def test_exact_digest_precedes_receipt_start_but_call_spans_receipt() -> None:
    # Capture fires before the delegation script records started_at_ms.
    req = request(
        (event(90), event(210, "PostToolUse"), event(300, tool="Edit", call="edit"))
    )
    followup = HandlerDelegationHookFollowup().handle(req).followups[0]
    assert followup.join == "digest"
    assert followup.signal is not None
    assert followup.signal.tool_calls == 1


def test_capture_digest_is_not_a_command_string_hash() -> None:
    import hashlib

    tool_input = {"command": "delegate text", "timeout": 900000}
    captured_digest = hashlib.sha256(
        json.dumps(
            tool_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    legacy_digest = hashlib.sha256(tool_input["command"].encode()).hexdigest()
    assert captured_digest != legacy_digest
    events = (event(100, digest=captured_digest),)
    legacy = receipt(command_sha256=legacy_digest)
    assert (
        HandlerDelegationHookFollowup()
        .handle(request(events, legacy))
        .followups[0]
        .join
        == "time"
    )
    corrected = legacy.model_copy(update={"tool_input_sha256": captured_digest})
    assert (
        HandlerDelegationHookFollowup()
        .handle(request(events, corrected))
        .followups[0]
        .join
        == "digest"
    )


def test_same_timestamp_refusal_precedence_and_duplicate_delivery() -> None:
    events = (
        event(100),
        event(300, call="refused"),
        event(300, "PermissionDenied", call="refused"),
        event(300, "PostToolUseFailure", call="refused"),
    )
    handler = HandlerDelegationHookFollowup()
    expected = handler.handle(request(events))
    assert handler.handle(request(tuple(reversed(events)) + events)) == expected
    assert expected.followups[0].signal is not None
    assert expected.followups[0].signal.refusals == 1


def test_repeated_work_uses_reader_rare_digest_threshold() -> None:
    shared = tuple(f"{i:064x}" for i in range(1, 6))
    events = [event(100)]
    for agent in ("agent-1", "peer"):
        for index, digest in enumerate(shared):
            events.extend(
                (
                    event(
                        300 + index * 10,
                        agent=agent,
                        call=f"call-{index}",
                        digest=digest,
                    ),
                    event(
                        301 + index * 10,
                        "PostToolUse",
                        agent=agent,
                        call=f"call-{index}",
                    ),
                )
            )
    signal = (
        HandlerDelegationHookFollowup()
        .handle(request(tuple(events)))
        .followups[0]
        .signal
    )
    assert signal is not None
    assert signal.repeated_work_agents == ("session-1/peer",)
    # Four shared digests alone do not establish repeated work.
    fewer = tuple(event for event in events if event.tool_use_id != "call-4")
    signal = HandlerDelegationHookFollowup().handle(request(fewer)).followups[0].signal
    assert signal is not None
    assert signal.repeated_work_agents == ()


def test_reuses_captured_hook_fixture_digest_and_main_thread_identity() -> None:
    fixture = (
        Path(__file__).parents[2]
        / "fixtures/claude_hook_capture/events/PreToolUse.json"
    )
    raw = json.loads(fixture.read_text())
    start = int(datetime.fromisoformat(raw["emitted_at"]).timestamp() * 1000)
    captured = ModelHookEvent(
        timestamp_ms=start,
        session_id=raw["lineage"]["session_id"],
        agent_id=raw["lineage"]["agent_id"],
        hook_event_name=raw["hook_event_name"],
        tool_name=raw["payload"]["tool_name"],
        tool_use_id=raw["lineage"]["tool_use_id"],
        tool_input_sha256=raw["payload"]["tool_input_ref"]["sha256"],
    )
    req = ModelHookFollowupRequest(
        receipts=(
            receipt(
                started_at_ms=start,
                ended_at_ms=start + 100,
                command_sha256=captured.tool_input_sha256,
                hook_session_id=captured.session_id,
                hook_agent_id=None,
            ),
        ),
        hook_events=(captured,),
        lane_rows=(
            ModelHookLaneRow(timestamp_ms=start + 1000, kind="TERMINAL", lane="lane-1"),
        ),
        window_start_ms=start,
        window_end_ms=start + 2000,
    )
    followup = HandlerDelegationHookFollowup().handle(req).followups[0]
    assert followup.join == "digest"
    assert followup.signal is not None
    assert followup.signal.agent_id is None
    assert followup.signal.tool_calls == 0


@pytest.mark.parametrize("call_id", [None, ""])
def test_events_without_call_id_do_not_create_calls(call_id: str | None) -> None:
    missing_id = event(300, tool="Edit").model_copy(update={"tool_use_id": call_id})
    events = (event(100), missing_id, event(400, tool="Write", call="write"))
    signal = HandlerDelegationHookFollowup().handle(request(events)).followups[0].signal
    assert signal is not None
    assert (signal.tool_calls, signal.edit_count, signal.write_count) == (1, 0, 1)


def test_unrelated_hook_does_not_complete_pending_call() -> None:
    events = (
        event(100),
        event(300, tool="Edit", call="edit", digest=OTHER),
        event(310, "Stop", tool="Edit", call="edit", digest=OTHER),
        event(400, "UserPromptSubmit", tool="Write", call="prompt"),
    )
    calls = fold(events)[("session-1", "agent-1")]
    assert [(call.tool_use_id, call.outcome) for call in calls] == [
        ("delegate", "pending"),
        ("edit", "pending"),
    ]
    signal = HandlerDelegationHookFollowup().handle(request(events)).followups[0].signal
    assert signal is not None
    assert (signal.tool_calls, signal.edit_count, signal.write_count) == (1, 1, 0)
    assert (signal.failures, signal.refusals, signal.retries) == (0, 0, 0)


@pytest.mark.parametrize(
    ("hook", "failures", "refusals"),
    [("PostToolUse", 0, 0), ("PostToolUseFailure", 1, 0), ("PermissionDenied", 0, 1)],
)
def test_completion_without_start_has_no_invented_digest(
    hook: str, failures: int, refusals: int
) -> None:
    events = (
        event(100),
        event(300, hook, tool="Write", call="orphan", digest=OTHER),
        event(400, tool="Write", call="next", digest=OTHER),
        event(410, "PostToolUse", tool="Write", call="next", digest=OTHER),
    )
    orphan = fold(events)[("session-1", "agent-1")][1]
    assert orphan.start == orphan.end == 300
    assert orphan.sha is None
    signal = HandlerDelegationHookFollowup().handle(request(events)).followups[0].signal
    assert signal is not None
    assert (signal.tool_calls, signal.write_count) == (2, 2)
    assert (signal.failures, signal.refusals) == (failures, refusals)
    assert signal.retries == 0
    assert signal.repeated_digests == ()


@pytest.mark.parametrize("missing_tool", [None, ""])
def test_completion_recovers_missing_tool_name(missing_tool: str | None) -> None:
    unknown = event(300, call="edit", digest=OTHER).model_copy(
        update={"tool_name": missing_tool}
    )
    events = (event(100), unknown, event(310, "PostToolUse", tool="Edit", call="edit"))
    signal = HandlerDelegationHookFollowup().handle(request(events)).followups[0].signal
    assert signal is not None
    assert (signal.tool_calls, signal.edit_count, signal.write_count) == (1, 1, 0)


def test_conflicting_prs_at_first_terminal_make_followup_ambiguous() -> None:
    rows = (
        ModelHookLaneRow(
            timestamp_ms=500, kind="TERMINAL", lane="lane-1", pr="omnimarket#123"
        ),
        ModelHookLaneRow(
            timestamp_ms=500, kind="TERMINAL", lane="lane-1", pr="omnimarket#999"
        ),
    )
    events = (event(100), event(300, tool="Edit", call="edit"))
    handler = HandlerDelegationHookFollowup()
    followup = handler.handle(request(events, rows=rows)).followups[0]
    assert followup.receipt_key == "receipt-1"
    assert followup.join == "ambiguous"
    assert followup.signal is None
    assert handler.handle(request(events, rows=tuple(reversed(rows)))).followups[0] == (
        followup
    )


def test_same_pr_at_first_terminal_and_different_later_pr_are_unambiguous() -> None:
    terminal = ModelHookLaneRow(
        timestamp_ms=500, kind="TERMINAL", lane="lane-1", pr="omnimarket#123"
    )
    rows = (
        terminal.model_copy(update={"timestamp_ms": 800, "pr": "omnimarket#999"}),
        terminal,
        terminal,
    )
    events = (
        event(100),
        event(300, tool="Edit", call="edit"),
        event(600, tool="Write", call="late"),
    )
    followup = (
        HandlerDelegationHookFollowup().handle(request(events, rows=rows)).followups[0]
    )
    assert followup.join == "digest"
    assert followup.signal is not None
    assert followup.signal.window_end_ms == 500
    assert (followup.signal.tool_calls, followup.signal.write_count) == (1, 0)


def test_capture_start_after_receipt_start_suppresses_signal() -> None:
    req = request((event(150), event(300, tool="Edit", call="edit")))
    req = ModelHookFollowupRequest.model_validate(
        {**req.model_dump(), "window_start_ms": 125}
    )
    followup = HandlerDelegationHookFollowup().handle(req).followups[0]
    assert followup.join == "digest"
    assert followup.signal is None


def test_receipt_rejects_reversed_window() -> None:
    with pytest.raises(ValidationError, match="receipt start must not follow its end"):
        receipt(started_at_ms=201, ended_at_ms=200)


def test_capture_rejects_reversed_window() -> None:
    req = request(())
    with pytest.raises(ValidationError, match="capture start must not follow its end"):
        ModelHookFollowupRequest.model_validate(
            {**req.model_dump(), "window_start_ms": 2001, "window_end_ms": 2000}
        )
