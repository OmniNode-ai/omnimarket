# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick prints the ruling register's MISSING lines, byte-for-byte (OMN-24478).

A standing ruling the register judged FALSE or UNREAD prints one MISSING `ruling-not-running:<row>`
line through the tick's report and lands in `missing`; a TRUE row prints nothing; the absent field
leaves the check out of `checked`. A register the facts read could not read is the reason on
`ruling_register_unread`: one MISSING `ruling-not-running:register` line, never a silent skip. The
read's journal write failures (`ruling_register_sink_errors`, OMN-24556) print one UNKNOWN
`ruling-register-journal` line beside the register's own findings, and `checked` still carries
`ruling-register` once. The
seam tests run the real compute handler (and, end to end, the real facts handler) and hold the
tick's lines to `result.lines()` of its result, so the compute line shape and the tick line shape
cannot drift apart.
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
from omnimarket.nodes.node_throughput_tick_facts_effect.handlers.handler_throughput_tick_facts import (
    HandlerThroughputTickFacts,
)
from omnimarket.nodes.node_throughput_tick_facts_effect.handlers.tick_facts_ports import (
    ScriptedTickFactsSource,
)
from omnimarket.nodes.node_throughput_tick_facts_effect.models.model_throughput_tick_facts import (
    ModelThroughputTickFactsRequest,
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
        read=ModelRulingRead(
            host="host-a",
            argv=("echo", row_id),
            # exit_zero names the nonzero code that reads FALSE.
            ok_exit_codes=(0, 1) if expect.kind is EnumRulingExpect.EXIT_ZERO else (0,),
            expect=expect,
        ),
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
                allowed_commands=tuple({row.read.argv[0] for row in policy_rows}),
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


def test_ruling_register_unread_prints_one_flattened_register_line() -> None:
    result = run(
        ruling_register_unread=(
            "NodeOverlayError: no node_ruling_register_compute overlay\n"
            "under ONEX_SKILL_OVERLAY_ROOTS=unset"
        )
    )
    (line,) = ruling_lines(result)
    assert line == (
        f"MISSING {LOOP_PREFIX}:register | the standing-rulings register could not be "
        "read: NodeOverlayError: no node_ruling_register_compute overlay under "
        "ONEX_SKILL_OVERLAY_ROOTS=unset | FIX: supply the node_ruling_register_compute "
        "overlay on this host's overlay roots"
    )
    assert ruling_missing(result) == [f"{LOOP_PREFIX}:register"]
    assert result.checked.count("ruling-register") == 1
    assert result.exit_code == 1


def test_ruling_register_a_result_beats_the_unread_reason() -> None:
    alpha, beta, gamma = rows()
    result = run(
        ruling_register=register(
            (alpha, beta, gamma),
            (reading(alpha.row_id), reading(beta.row_id, stdout="5")),
        ),
        ruling_register_unread="a stale reason from an earlier read",
    )
    assert result.checked.count("ruling-register") == 1
    assert ruling_missing(result) == [
        f"{LOOP_PREFIX}:{beta.row_id}",
        f"{LOOP_PREFIX}:{gamma.row_id}",
    ]


# ---- the facts effect -> tick decision seam (OMN-24478) -------------------------------------------


class _StaticRegisterSource:
    """The facts handler's register port, answered from one fixed result and its
    journal write failures."""

    def __init__(
        self,
        result: ModelRulingRegisterResult,
        sink_errors: tuple[str, ...] = (),
    ) -> None:
        self._result = result
        self._sink_errors = sink_errors

    def read(
        self, overlay_roots: str
    ) -> tuple[ModelRulingRegisterResult, tuple[str, ...]]:
        return self._result, self._sink_errors


def tick_facts(
    judged: ModelRulingRegisterResult,
    sink_errors: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The facts the facts effect reads, dumped the way the runtime builds the tick
    request: ``model_dump(exclude_unset=True)`` of its facts model."""
    return (
        HandlerThroughputTickFacts(
            ScriptedTickFactsSource(),
            ruling_register=_StaticRegisterSource(judged, sink_errors),
        )
        .handle(
            ModelThroughputTickFactsRequest(
                now=NOW, landing_state_dir="/l", state_dir="/s", open_basis="b"
            )
        )
        .facts.model_dump(exclude_unset=True)
    )


def judged_with_one_false_and_one_unread_row() -> ModelRulingRegisterResult:
    alpha, beta, gamma = rows()
    return register(
        (alpha, beta, gamma), (reading(alpha.row_id), reading(beta.row_id, stdout="5"))
    )


def test_seam_the_facts_effects_register_prints_the_rows_missing_lines() -> None:
    judged = judged_with_one_false_and_one_unread_row()
    result = run(**tick_facts(judged))
    assert ruling_lines(result) == list(judged.lines())
    assert ruling_missing(result) == [
        f"{LOOP_PREFIX}:row-beta",
        f"{LOOP_PREFIX}:row-gamma",
    ]
    assert "ruling-register" in result.checked


def test_seam_failing_control_no_register_fact_prints_nothing() -> None:
    """The failing control: with the register fact removed (the state before OMN-24478),
    the same facts print no ruling line and the check stays out of `checked`."""
    facts = tick_facts(judged_with_one_false_and_one_unread_row())
    facts.pop("ruling_register")
    result = run(**facts)
    assert ruling_lines(result) == []
    assert "ruling-register" not in result.checked


# ---- the register read's journal write failures (OMN-24556) ----------------------------------------


def all_true() -> ModelRulingRegisterResult:
    """The judged register of the three rows, every row TRUE."""
    alpha, beta, gamma = rows()
    return register(
        (alpha, beta, gamma),
        (
            reading(alpha.row_id),
            reading(beta.row_id, stdout="0"),
            reading(gamma.row_id),
        ),
    )


def journal_lines(result: ModelThroughputTickResult) -> list[str]:
    return [line for line in result.lines if "ruling-register-journal" in line]


def test_sink_errors_a_read_register_prints_one_journal_unknown() -> None:
    result = run(
        ruling_register=all_true(),
        ruling_register_sink_errors=["row-a: OSError", "row-b: OSError"],
    )
    (line,) = journal_lines(result)
    assert line == (
        "UNKNOWN ruling-register-journal | the register's journal did not take "
        "2 read event(s): row-a: OSError; row-b: OSError | FIX: make the journal path "
        "the effect route's journal_env names writable on the tick host"
    )
    assert result.unknown.count("ruling-register-journal") == 1
    assert result.checked.count("ruling-register") == 1
    assert result.exit_code == 1


def test_sink_errors_an_unread_register_prints_the_register_line_and_the_journal() -> (
    None
):
    result = run(
        ruling_register_unread=(
            "NodeOverlayError: no node_ruling_register_compute overlay\n"
            "under ONEX_SKILL_OVERLAY_ROOTS=unset"
        ),
        ruling_register_sink_errors=["row-a: OSError"],
    )
    assert ruling_missing(result) == [f"{LOOP_PREFIX}:register"]
    assert result.unknown.count("ruling-register-journal") == 1
    assert result.checked.count("ruling-register") == 1
    assert result.exit_code == 1


def test_sink_errors_positive_control_no_journal_failures_print_nothing_extra() -> None:
    result = run(ruling_register=all_true())
    assert journal_lines(result) == []
    assert "ruling-register-journal" not in result.unknown
    assert result.checked.count("ruling-register") == 1
    assert ruling_missing(result) == []


def test_sink_errors_seam_the_facts_effects_journal_failures_print_the_unknown() -> (
    None
):
    judged = judged_with_one_false_and_one_unread_row()
    result = run(**tick_facts(judged, ("row-a: OSError",)))
    (line,) = journal_lines(result)
    assert line == (
        "UNKNOWN ruling-register-journal | the register's journal did not take "
        "1 read event(s): row-a: OSError | FIX: make the journal path "
        "the effect route's journal_env names writable on the tick host"
    )
    assert result.checked.count("ruling-register") == 1
    assert ruling_missing(result) == [
        f"{LOOP_PREFIX}:row-beta",
        f"{LOOP_PREFIX}:row-gamma",
    ]


def test_sink_errors_seam_positive_control_the_taken_journal_prints_nothing() -> None:
    result = run(**tick_facts(judged_with_one_false_and_one_unread_row()))
    assert journal_lines(result) == []
    assert "ruling-register-journal" not in result.unknown
