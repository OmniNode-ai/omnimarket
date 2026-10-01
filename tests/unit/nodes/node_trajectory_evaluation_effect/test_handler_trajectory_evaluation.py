# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler tests for node_trajectory_evaluation_effect (OMN-20087)."""

from __future__ import annotations

import socket
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_trajectory_evaluation_effect.handlers.handler_trajectory_evaluation import (
    HandlerTrajectoryEvaluation,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.models.model_trajectory_evaluation import (
    ModelTrajectoryEvaluationAccepted,
    ModelTrajectoryEvaluationFailed,
    ModelTrajectoryEvaluationRefused,
    ModelTrajectoryEvaluationStatus,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.protocols import (
    EvaluatorRateLimitedError,
    EvaluatorRejectedError,
    EvaluatorTransportError,
)
from tests.unit.nodes.node_trajectory_evaluation_effect.fakes import (
    SETTING,
    full_cmd,
    make_handler,
    poll_cmd,
    submit_cmd,
)

pytestmark = pytest.mark.unit


def _refused(outcome: Any, reason: str) -> None:
    assert isinstance(outcome, ModelTrajectoryEvaluationRefused), outcome
    assert outcome.reason == reason


async def test_disabled_when_setting_unset() -> None:
    handler, github, ev, _ = make_handler(None)
    _refused(await handler.handle(submit_cmd()), "backend_disabled")
    assert github.requests == []
    assert ev.submissions == []


async def test_disabled_when_setting_off_or_blank() -> None:
    for value in ("off", "  ", ""):
        handler, github, ev, _ = make_handler(value)
        _refused(await handler.handle(submit_cmd()), "backend_disabled")
        assert github.requests == []
        assert ev.submissions == []


async def test_disabled_poll_is_refused() -> None:
    handler, github, ev, _ = make_handler(None)
    _refused(await handler.handle(poll_cmd()), "backend_disabled")
    assert github.requests == []
    assert ev.reads == 0


@pytest.mark.parametrize(
    "value",
    ["bogus", "IN_MEMORY", "dharma", "dharma_", "a_b_full", "in-memory", "1x_full"],
)
async def test_misconfigured_setting(value: str) -> None:
    handler, github, ev, _ = make_handler(value)
    _refused(await handler.handle(submit_cmd()), "backend_misconfigured")
    assert github.requests == []
    assert ev.submissions == []


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"work_unit_repository": None}, id="missing_repository"),
        pytest.param({"work_unit_repository": "../x"}, id="dotdot_owner"),
        pytest.param({"work_unit_repository": "x/.."}, id="dotdot_name"),
        pytest.param({"work_unit_repository": "a/b/c"}, id="three_segments"),
        pytest.param({"work_unit_repository": "a/b?c=d"}, id="query_chars"),
        pytest.param({"content_mode": "bogus"}, id="bad_mode"),
        pytest.param({"content_mode": None}, id="no_mode"),
        pytest.param(
            {"content_mode": "full", "prompt_text": "p", "response_text": None},
            id="full_no_response",
        ),
        pytest.param(
            {"content_mode": "full", "prompt_text": None, "response_text": "r"},
            id="full_no_prompt",
        ),
    ],
)
async def test_input_invalid(overrides: dict[str, Any]) -> None:
    handler, github, ev, _ = make_handler("in_memory")
    _refused(await handler.handle(submit_cmd(**overrides)), "input_invalid")
    assert github.requests == []
    assert ev.submissions == []


async def test_idempotency_key_and_duplicate_flag() -> None:
    handler, _, ev, _ = make_handler("in_memory")
    cmd = submit_cmd()
    first = await handler.handle(cmd)
    second = await handler.handle(cmd)
    assert isinstance(first, ModelTrajectoryEvaluationAccepted)
    assert isinstance(second, ModelTrajectoryEvaluationAccepted)
    assert first.backend_receipt_id == second.backend_receipt_id
    assert (first.duplicate, second.duplicate) == (False, True)
    assert ev.submissions[0].idempotency_key == f"onex-eval-meta-{cmd.correlation_id}"
    full = full_cmd()
    await handler.handle(full)
    assert ev.submissions[-1].idempotency_key == f"onex-eval-full-{full.correlation_id}"
    assert 8 <= len(ev.submissions[0].idempotency_key) <= 200


async def test_content_mode_in_memory_accepts_both() -> None:
    handler, _, _, _ = make_handler("in_memory")
    assert isinstance(
        await handler.handle(submit_cmd()), ModelTrajectoryEvaluationAccepted
    )
    assert isinstance(
        await handler.handle(full_cmd()), ModelTrajectoryEvaluationAccepted
    )


async def test_content_mode_not_permitted_full_under_metadata_backend() -> None:
    handler, github, ev, _ = make_handler("dharma_metadata")
    _refused(await handler.handle(full_cmd()), "content_mode_not_permitted")
    assert github.requests == []
    assert ev.submissions == []


@pytest.mark.parametrize(
    ("setting", "cmd"),
    [
        ("dharma_metadata", submit_cmd),
        ("dharma_full", submit_cmd),
        ("dharma_full", full_cmd),
    ],
)
async def test_content_mode_permitted_but_external_backend_unavailable(
    setting: str, cmd: Any
) -> None:
    handler, github, ev, _ = make_handler(setting)
    _refused(await handler.handle(cmd()), "backend_unavailable")
    assert github.requests == []
    assert ev.submissions == []


async def test_bounded_poll_completes_on_third_read() -> None:
    handler, _, ev, clock = make_handler("in_memory")
    ev.terminal_after_reads = 3
    out = await handler.handle(submit_cmd())
    assert isinstance(out, ModelTrajectoryEvaluationAccepted)
    assert out.verdict_state == "completed"
    assert out.verdict is not None
    assert ev.reads == 3
    assert clock.sleeps == [10, 10]


async def test_bounded_poll_pending_after_thirteen_reads() -> None:
    handler, _, ev, clock = make_handler("in_memory")
    ev.terminal_after_reads = 10**9
    out = await handler.handle(submit_cmd())
    assert isinstance(out, ModelTrajectoryEvaluationAccepted)
    assert out.verdict_state == "pending"
    assert out.verdict is None
    assert ev.reads == 13
    assert clock.sleeps == [10] * 12


@pytest.mark.parametrize(
    "receipt", ["../x", "a?b", "a/b", "", None, "x" * 201, "a..b/"]
)
async def test_poll_input_invalid_receipt(receipt: str | None) -> None:
    handler, github, ev, _ = make_handler("in_memory")
    _refused(
        await handler.handle(poll_cmd(backend_receipt_id=receipt)), "input_invalid"
    )
    assert github.requests == []
    assert ev.reads == 0


async def test_poll_bad_content_mode_is_input_invalid() -> None:
    handler, _, ev, _ = make_handler("in_memory")
    _refused(await handler.handle(poll_cmd(content_mode="x")), "input_invalid")
    assert ev.reads == 0


async def test_poll_two_reads_of_terminal_receipt_are_byte_identical() -> None:
    handler, _, ev, _ = make_handler("in_memory")
    ev.terminal_after_reads = 1
    accepted = await handler.handle(submit_cmd())
    assert isinstance(accepted, ModelTrajectoryEvaluationAccepted)
    cid = uuid4()
    cmd = poll_cmd(correlation_id=cid, backend_receipt_id=accepted.backend_receipt_id)
    one, two = await handler.handle(cmd), await handler.handle(cmd)
    assert isinstance(one, ModelTrajectoryEvaluationStatus)
    assert one.verdict_state == "completed"
    assert one.model_dump_json() == two.model_dump_json()


async def test_poll_unknown_receipt_is_refused_target_rejected() -> None:
    handler, _, _, _ = make_handler("in_memory")
    _refused(
        await handler.handle(poll_cmd(backend_receipt_id="mem-nope")), "target_rejected"
    )


async def test_evaluator_errors_map_to_outcomes() -> None:
    handler, _, ev, _ = make_handler("in_memory")
    ev.fail_with = EvaluatorRateLimitedError("slow down", status_code=429)
    out = await handler.handle(submit_cmd())
    assert isinstance(out, ModelTrajectoryEvaluationFailed)
    assert out.reason == "evaluator_rate_limited"
    assert out.retryable
    ev.fail_with = EvaluatorTransportError("boom")
    out = await handler.handle(submit_cmd())
    assert isinstance(out, ModelTrajectoryEvaluationFailed)
    assert out.reason == "evaluator_transport"
    ev.fail_with = EvaluatorRejectedError("no", status_code=422)
    _refused(await handler.handle(submit_cmd()), "target_rejected")


async def test_outcomes_carry_no_wall_clock_field() -> None:
    for model in (
        ModelTrajectoryEvaluationAccepted,
        ModelTrajectoryEvaluationRefused,
        ModelTrajectoryEvaluationFailed,
        ModelTrajectoryEvaluationStatus,
    ):
        assert not {n for n in model.model_fields if "time" in n or n.endswith("_at")}


async def test_one_log_line_per_outbound_call_without_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler, _, _, _ = make_handler("in_memory")
    with caplog.at_level("INFO"):
        cmd = full_cmd()
        await handler.handle(cmd)
    lines = [
        r.getMessage() for r in caplog.records if "outbound_call" in r.getMessage()
    ]
    assert any(
        "operation=repository_read" in x and "host=" in x and "status=200" in x
        for x in lines
    )
    assert any("operation=evaluator_submit" in x for x in lines)
    assert all(str(cmd.correlation_id) in x for x in lines)
    assert not any("authorization" in x.lower() or "prompt" in x.lower() for x in lines)


def test_construct_bare_makes_no_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SETTING, raising=False)

    def _no_network(*a: Any, **k: Any) -> None:
        raise AssertionError("network touched during construction")

    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    handler = HandlerTrajectoryEvaluation()
    assert handler is not None
