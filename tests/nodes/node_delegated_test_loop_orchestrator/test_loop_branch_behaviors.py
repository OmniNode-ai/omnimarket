# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protect reply rejection, terminal bounds and receipt replay at the loop seams."""

from __future__ import annotations

import json

import pytest

from omnimarket.nodes.node_delegated_test_loop_orchestrator.handlers.handler_delegated_test_loop_orchestrator import (
    MAX_HOST_BUSY_TRIES,
    HandlerDelegatedTestLoopOrchestrator,
    excerpt_lines,
)
from omnimarket.nodes.node_delegated_test_loop_orchestrator.handlers.replay import (
    replay_loop_receipt,
)
from omnimarket.nodes.node_delegated_test_loop_orchestrator.handlers.reply_parser import (
    parse_test_reply,
)
from omnimarket.nodes.node_delegated_test_loop_orchestrator.models.model_delegated_test_loop import (
    EnumLoopStatus,
    ModelControlVerdict,
    ModelDelegateReply,
)
from omnimarket.nodes.node_delegated_test_loop_orchestrator.protocols.protocol_delegated_test_loop_ports import (
    ModelPrompt,
)
from tests.nodes.node_delegated_test_loop_orchestrator.test_handler_delegated_test_loop_orchestrator import (
    CORRELATION,
    FIXED,
    FakePorts,
    _request,
)
from tests.nodes.node_delegated_test_loop_orchestrator.test_loop_code_gate_repair import (
    GatedPorts,
)

pytestmark = pytest.mark.unit

PATH = "tests/unit/test_generated.py"
SOURCE = "def test_ok():\n    assert True\n"


@pytest.mark.parametrize("text", ["", "   ", "{", "}", "{broken}", '{"test_source": }'])
def test_empty_or_malformed_json_reply_is_rejected_with_a_reason(text: str) -> None:
    assert parse_test_reply(text, PATH) == (
        "",
        "the reply is not a JSON object with test_path and test_source",
    )


@pytest.mark.parametrize("source", [None, 42, [], {}, "", " \t\n"])
def test_missing_non_string_or_blank_source_is_never_a_test(source: object) -> None:
    assert parse_test_reply(json.dumps({"test_source": source}), PATH) == (
        "",
        "the JSON object has no test_source string",
    )


def test_malformed_fence_is_skipped_for_the_next_valid_fence() -> None:
    reply = json.dumps({"test_path": PATH, "test_source": SOURCE})
    text = f'```json\n{{"test_source": }}\n```\n```json\n{reply}\n```'
    assert parse_test_reply(text, PATH) == (SOURCE, "")


@pytest.mark.parametrize("path_fields", [{}, {"test_path": None}])
def test_a_reply_may_omit_the_path_or_use_null(path_fields: dict[str, object]) -> None:
    assert parse_test_reply(
        json.dumps({**path_fields, "test_source": SOURCE}), PATH
    ) == (
        SOURCE,
        "",
    )


@pytest.mark.parametrize("steps", [None, {}, (), ""])
def test_replay_refuses_a_non_list_step_sequence(steps: object) -> None:
    with pytest.raises(ValueError, match="a loop receipt's steps must be a list"):
        replay_loop_receipt(
            {"request": _request().model_dump(mode="json"), "steps": steps}
        )


def test_replay_ignores_non_step_values_and_unknown_step_kinds() -> None:
    ports = FakePorts({"fixed": ["passed"], "prefix": ["failed_call"]})
    HandlerDelegatedTestLoopOrchestrator(ports).run(_request())
    payload = ports.written[CORRELATION]
    steps = payload["steps"]
    assert isinstance(steps, list)
    payload["steps"] = [None, "annotation", {"kind": "annotation"}, *steps, 42]
    assert replay_loop_receipt(payload) is EnumLoopStatus.ACCEPTED_CALL


@pytest.mark.parametrize("digest", [None, [], "not a digest"])
def test_replay_refuses_a_completed_run_without_a_digest(digest: object) -> None:
    ports = FakePorts({"fixed": ["passed"], "prefix": ["failed_call"]})
    HandlerDelegatedTestLoopOrchestrator(ports).run(_request())
    payload = ports.written[CORRELATION]
    steps = payload["steps"]
    assert isinstance(steps, list)
    run = next(step for step in steps if step["kind"] == "run")
    run["digest"] = digest
    with pytest.raises(KeyError, match=run["receipt_id"]):
        replay_loop_receipt(payload)


@pytest.mark.parametrize("bound", [1, 2, 3])
def test_blank_replies_exhaust_the_requested_bound_without_running_tests(
    bound: int,
) -> None:
    replies = [
        ModelDelegateReply(
            run_id=f"blank-{attempt}",
            ok=True,
            test_source=" \n",
            invalid_reason=f"malformed reply {attempt}",
        )
        for attempt in range(1, bound + 1)
    ]
    ports = FakePorts({}, replies=replies)
    result = HandlerDelegatedTestLoopOrchestrator(ports).run(
        _request(max_delegate_calls=bound)
    )
    assert result.status is EnumLoopStatus.FAILED
    assert result.attempts == ports.delegate_calls == bound
    assert ports.run_calls == []
    assert result.headline is False
    assert result.control is None
    assert result.detail == "the attempt bound was reached without a pass"
    assert result.final_digest is not None
    assert result.final_digest.message == f"malformed reply {bound}"
    assert replay_loop_receipt(ports.written[CORRELATION]) is EnumLoopStatus.FAILED


def test_repeated_invalid_reply_stops_for_no_progress_before_the_bound() -> None:
    ports = FakePorts(
        {}, replies=[ModelDelegateReply(run_id=f"r{i}", ok=False) for i in range(3)]
    )
    result = HandlerDelegatedTestLoopOrchestrator(ports).run(
        _request(max_delegate_calls=2)
    )
    assert result.status is EnumLoopStatus.NO_PROGRESS
    assert result.attempts == ports.delegate_calls == 2
    assert ports.run_calls == []
    assert result.final_digest is not None
    assert result.final_digest.message == "the delegate call returned no usable test"
    assert replay_loop_receipt(ports.written[CORRELATION]) is EnumLoopStatus.NO_PROGRESS


@pytest.mark.parametrize("receipt_fault", [False, True])
def test_replay_preserves_run_and_digest_infrastructure_terminals(
    receipt_fault: bool,
) -> None:
    ports = FakePorts(
        {"fixed": ["infra_error"]},
        receipt_status="infra_error" if receipt_fault else "completed",
    )
    result = HandlerDelegatedTestLoopOrchestrator(ports).run(_request())
    assert result.status is EnumLoopStatus.INFRA_ERROR
    assert result.headline is False
    assert result.control is None
    assert ports.run_calls == [("fixed", FIXED)]
    assert replay_loop_receipt(ports.written[CORRELATION]) is EnumLoopStatus.INFRA_ERROR


def test_replay_preserves_exhausted_host_busy_retries() -> None:
    ports = FakePorts({}, busy_first=MAX_HOST_BUSY_TRIES)
    result = HandlerDelegatedTestLoopOrchestrator(ports).run(_request())
    assert result.status is EnumLoopStatus.HOST_BUSY
    assert len(ports.run_calls) == MAX_HOST_BUSY_TRIES
    assert result.run_receipt_ids == ()
    assert replay_loop_receipt(ports.written[CORRELATION]) is EnumLoopStatus.HOST_BUSY


def test_initial_prompt_refusal_is_terminal_before_any_delegate_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ports = FakePorts({})

    def refuse(*args: object, **kwargs: object) -> ModelPrompt:
        raise ValueError("prompt exceeds the context budget")

    monkeypatch.setattr(ports, "build_prompt", refuse)
    result = HandlerDelegatedTestLoopOrchestrator(ports).run(_request())
    assert result.status is EnumLoopStatus.INFRA_ERROR
    assert result.detail == "prompt refused: prompt exceeds the context budget"
    assert result.attempts == ports.delegate_calls == 0
    assert ports.run_calls == []
    assert ports.written[CORRELATION]["steps"] == []


def test_unknown_control_grade_is_an_infrastructure_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ports = FakePorts({"fixed": ["passed"], "prefix": ["failed_call"]})

    def unknown_grade(*args: object) -> ModelControlVerdict:
        return ModelControlVerdict(
            status="unknown",
            headline=True,
            control_ref_role="prefix",
            control_outcome="failed_call",
        )

    monkeypatch.setattr(ports, "grade", unknown_grade)
    result = HandlerDelegatedTestLoopOrchestrator(ports).run(_request())
    assert result.status is EnumLoopStatus.INFRA_ERROR
    assert result.detail == "unknown grade 'unknown'"
    assert result.headline is False
    assert result.control is None
    assert replay_loop_receipt(ports.written[CORRELATION]) is EnumLoopStatus.INFRA_ERROR


def test_gate_repair_prompt_refusal_keeps_the_passing_test_and_records_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ports = GatedPorts({"fixed": ["passed"], "prefix": ["failed_call"]}, ["findings"])
    original = ports.build_prompt

    def refuse_repair(*args: object, **kwargs: object) -> ModelPrompt:
        if kwargs.get("gate") is not None:
            raise ValueError("repair prompt exceeds the context budget")
        return original(*args, **kwargs)

    monkeypatch.setattr(ports, "build_prompt", refuse_repair)
    result = HandlerDelegatedTestLoopOrchestrator(ports).run(_request())
    assert result.status is EnumLoopStatus.ACCEPTED_CALL
    assert result.attempts == ports.delegate_calls == 1
    assert result.gate_repairs == 1
    assert result.gate_clean is False
    assert result.gate_findings == 2
    assert ports.run_sources[-1] == ("prefix", ports.run_sources[0][1])
    steps = ports.written[CORRELATION]["steps"]
    assert isinstance(steps, list)
    assert [step for step in steps if step["kind"] == "gate_repair_kept"] == [
        {
            "kind": "gate_repair_kept",
            "attempt": 2,
            "reason": "repair prompt exceeds the context budget",
        }
    ]


def test_gate_findings_at_the_call_bound_do_not_trigger_a_repair() -> None:
    ports = GatedPorts({"fixed": ["passed"], "prefix": ["failed_call"]}, ["findings"])
    result = HandlerDelegatedTestLoopOrchestrator(ports).run(
        _request(max_delegate_calls=1)
    )
    assert result.status is EnumLoopStatus.ACCEPTED_CALL
    assert result.attempts == ports.delegate_calls == 1
    assert result.gate_repairs == 0
    assert result.gate_clean is False
    assert result.gate_findings == 2
    assert ports.run_sources[-1] == ("prefix", ports.run_sources[0][1])
    assert (
        replay_loop_receipt(ports.written[CORRELATION]) is EnumLoopStatus.ACCEPTED_CALL
    )


def test_infrastructure_failure_during_gate_repair_stops_before_control() -> None:
    ports = GatedPorts({"fixed": ["passed", "infra_error"]}, ["findings"])
    result = HandlerDelegatedTestLoopOrchestrator(ports).run(_request())
    assert result.status is EnumLoopStatus.INFRA_ERROR
    assert result.headline is False
    assert result.control is None
    assert result.attempts == ports.delegate_calls == 2
    assert result.gate_clean is False
    assert result.gate_repairs == 1
    assert [role for role, _ in ports.run_calls] == ["fixed", "fixed"]
    assert replay_loop_receipt(ports.written[CORRELATION]) is EnumLoopStatus.INFRA_ERROR


async def test_async_handle_returns_the_same_terminal_result() -> None:
    ports = FakePorts({}, replies=[ModelDelegateReply(run_id="empty", ok=False)])
    handler = HandlerDelegatedTestLoopOrchestrator(ports)
    assert handler.handler_type == "NODE_HANDLER"
    assert handler.handler_category == "ORCHESTRATOR"
    result = await handler.handle(_request(max_delegate_calls=1))
    assert result.status is EnumLoopStatus.FAILED
    assert ports.written[CORRELATION]["result"]["status"] == "failed"


def test_excerpt_skips_ranges_outside_the_target() -> None:
    assert excerpt_lines("one\ntwo\n", ((3, 5), (-3, 0), (2, 2)), "src/a.py") == (
        "# --- src/a.py lines 2-2 ---\ntwo\n"
    )
