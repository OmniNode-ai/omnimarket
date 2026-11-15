# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick prints the ruling register's MISSING lines, byte-for-byte (OMN-24478).

A standing ruling the register judged FALSE or UNREAD prints one MISSING `ruling-not-running:<row>`
line through the tick's report and lands in `missing`; a TRUE row prints nothing; the absent field
leaves the check out of `checked`. The seam test runs the real compute handler and holds the tick's
lines to `result.lines()` of its result, so the compute line shape and the tick line shape cannot
drift apart.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.models.model_ruling_register import (
    LOOP_PREFIX,
    EnumRulingExpect,
    EnumRulingVerdict,
    ModelRulingExpect,
    ModelRulingRead,
    ModelRulingReading,
    ModelRulingRegisterPolicy,
    ModelRulingRegisterRequest,
    ModelRulingRegisterResult,
    ModelRulingRow,
)
from omnimarket.nodes.node_ruling_register_compute.handlers.handler_ruling_register import (
    HandlerRulingRegister,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision import (
    HandlerThroughputTickDecision,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
    ModelThroughputTickRequest,
    ModelThroughputTickResult,
)

pytestmark = pytest.mark.unit

HANDLER = HandlerThroughputTickDecision()
COMPUTE = HandlerRulingRegister()
NOW = "2026-11-02T15:02:01Z"
READ_AT = "2026-11-02T15:01:01Z"
CORRELATION_ID = "corr-ruling-register-lines"
BOUND_SECONDS = 3600


def row(
    row_id: str,
    *,
    ruling: str,
    owner: str,
    fix: str,
    expect: ModelRulingExpect,
) -> ModelRulingRow:
    return ModelRulingRow(
        row_id=row_id,
        ruling=ruling,
        must_be_true=f"the {row_id} ruling holds on host-a",
        read=ModelRulingRead(host="host-a", argv=("echo", row_id), expect=expect),
        owner=owner,
        fix=fix,
    )


def rows() -> tuple[ModelRulingRow, ...]:
    """Three rows: alpha's read exits zero, beta's reads an int at most 2, gamma's reads empty."""
    return (
        row(
            "row-alpha",
            ruling="2026-11-02T15:02:01Z",
            owner="lane-alpha",
            fix="re-run row-alpha's read on host-a",
            expect=ModelRulingExpect(kind=EnumRulingExpect.EXIT_ZERO),
        ),
        row(
            "row-beta",
            ruling="2026-11-02T14:02:01Z",
            owner="lane-beta",
            fix="shrink row-beta's count on host-a",
            expect=ModelRulingExpect(kind=EnumRulingExpect.INT_AT_MOST, bound=2),
        ),
        row(
            "row-gamma",
            ruling="2026-11-02T13:02:01Z",
            owner="lane-gamma",
            fix="re-run row-gamma's read on host-a",
            expect=ModelRulingExpect(kind=EnumRulingExpect.STDOUT_EMPTY),
        ),
    )


def reading(row_id: str, **overrides: Any) -> ModelRulingReading:
    return ModelRulingReading.model_validate(
        {
            "row_id": row_id,
            "status": "ok",
            "exit_code": 0,
            "read_at": READ_AT,
            **overrides,
        }
    )


def unread_reading(row_id: str) -> ModelRulingReading:
    return reading(row_id, status="unread", detail="the read did not run")


def register(
    policy_rows: tuple[ModelRulingRow, ...],
    readings: tuple[ModelRulingReading, ...] = (),
) -> ModelRulingRegisterResult:
    """The compute node's judgement of the rows against the readings."""
    return COMPUTE.handle(
        ModelRulingRegisterRequest(
            correlation_id=CORRELATION_ID,
            now=NOW,
            policy=ModelRulingRegisterPolicy(
                max_reading_age_seconds=BOUND_SECONDS,
                rows=policy_rows,
            ),
            readings=readings,
        )
    )


def run(**overrides: Any) -> ModelThroughputTickResult:
    payload: dict[str, Any] = {"now": NOW, "launchctl": "not_loaded", **overrides}
    return HANDLER.handle(ModelThroughputTickRequest.model_validate(payload))


def ruling_missing(result: ModelThroughputTickResult) -> list[str]:
    return [m for m in result.missing if m.startswith(f"{LOOP_PREFIX}:")]


def ruling_lines(result: ModelThroughputTickResult) -> list[str]:
    return [line for line in result.lines if LOOP_PREFIX in line]


def test_ruling_register_golden_every_row_true_prints_nothing() -> None:
    alpha, beta, gamma = rows()
    result = run(
        ruling_register=register(
            (alpha, beta, gamma),
            (
                reading(alpha.row_id),
                reading(beta.row_id, stdout="0"),
                reading(gamma.row_id),
            ),
        )
    )
    assert ruling_lines(result) == []
    assert ruling_missing(result) == []
    assert "ruling-register" in result.checked


def test_ruling_register_a_false_row_and_an_unread_row_get_one_line_each() -> None:
    alpha, beta, gamma = rows()
    result = run(
        ruling_register=register(
            (alpha, beta, gamma),
            (reading(alpha.row_id), reading(beta.row_id, stdout="5")),
        )
    )
    lines = ruling_lines(result)
    assert len(lines) == 2, lines
    assert ruling_missing(result) == [
        f"{LOOP_PREFIX}:{beta.row_id}",
        f"{LOOP_PREFIX}:{gamma.row_id}",
    ]
    for line, not_running in zip(lines, (beta, gamma), strict=True):
        assert line.startswith(f"MISSING {LOOP_PREFIX}:{not_running.row_id} | "), line
        assert not_running.ruling in line, line
        assert f"owner {not_running.owner}" in line, line
        assert line.endswith(f" | FIX: {not_running.fix}"), line
    assert result.exit_code == 1


def test_ruling_register_without_the_field_the_tick_does_not_check_it() -> None:
    result = run()
    assert "ruling-register" not in result.checked
    assert ruling_lines(result) == []


def test_ruling_register_seam_tick_lines_equal_the_compute_result_lines() -> None:
    alpha, beta, gamma = rows()
    compute = register(
        (alpha, beta, gamma),
        (reading(alpha.row_id), reading(beta.row_id, stdout="5")),
    )
    assert [verdict.verdict for verdict in compute.verdicts] == [
        EnumRulingVerdict.TRUE,
        EnumRulingVerdict.FALSE,
        EnumRulingVerdict.UNREAD,
    ]
    assert len(compute.lines()) == 2
    result = run(ruling_register=compute)
    assert ruling_lines(result) == list(compute.lines())


def test_ruling_failing_control_an_unread_row_is_never_silent() -> None:
    only = row(
        "row-only",
        ruling="2026-11-02T15:02:01Z",
        owner="lane-only",
        fix="re-run row-only's read on host-a",
        expect=ModelRulingExpect(kind=EnumRulingExpect.EXIT_ZERO),
    )
    result = run(ruling_register=register((only,), (unread_reading(only.row_id),)))
    lines = ruling_lines(result)
    assert len(lines) == 1, lines
    assert lines[0].startswith(f"MISSING {LOOP_PREFIX}:{only.row_id} | "), lines[0]
    assert ruling_missing(result) == [f"{LOOP_PREFIX}:{only.row_id}"]
    assert result.exit_code == 1
