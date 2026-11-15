# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ruling register judge (OMN-24478): readings judged against the register, one verdict per row.

A row that took no reading, read unread, read stale or future, or answered in a shape its judge
cannot parse is UNREAD, never TRUE. ``passed`` holds only when every row is TRUE, and a FALSE or
UNREAD row reads one MISSING line naming the ruling stamp, its owner and its fix. The node judges
the same request as its handler, and the overlay loader returns the typed register or refuses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_ruling_register import (
    EnumReadStatus,
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
from omnimarket.nodes.node_ruling_register_compute import NodeRulingRegisterCompute
from omnimarket.nodes.node_ruling_register_compute.handlers.handler_ruling_register import (
    HandlerRulingRegister,
)
from omnimarket.nodes.node_ruling_register_compute.overlay import (
    load_ruling_register_policy,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

NOW = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)
RULING = "2026-07-01T10:00:00Z"
MAX_AGE = 600


def row(
    row_id: str = "row-1",
    kind: str = "exit_zero",
    value: str = "",
    bound: int | None = None,
    owner: str = "example-lane",
) -> ModelRulingRow:
    return ModelRulingRow(
        row_id=row_id,
        ruling=RULING,
        must_be_true="the example ruling holds",
        read=ModelRulingRead(
            host="host-a",
            argv=("example-command",),
            expect=ModelRulingExpect(
                kind=EnumRulingExpect(kind), value=value, bound=bound
            ),
        ),
        owner=owner,
        fix=f"fix {row_id}",
    )


def reading(
    row_id: str = "row-1",
    status: EnumReadStatus = EnumReadStatus.OK,
    exit_code: int | None = 0,
    stdout: str = "",
    age_seconds: int = 60,
    detail: str = "",
) -> ModelRulingReading:
    return ModelRulingReading(
        row_id=row_id,
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        read_at=NOW - timedelta(seconds=age_seconds),
        detail=detail,
    )


def judge(
    rows: tuple[ModelRulingRow, ...], *readings: ModelRulingReading
) -> ModelRulingRegisterResult:
    return HandlerRulingRegister().handle(
        ModelRulingRegisterRequest(
            correlation_id="ruling-1",
            now=NOW,
            policy=ModelRulingRegisterPolicy(
                max_reading_age_seconds=MAX_AGE, rows=rows
            ),
            readings=readings,
        )
    )


def test_golden_a_register_whose_rows_all_hold_prints_no_line() -> None:
    result = judge(
        (
            row("row-ok"),
            row("row-empty", kind="stdout_empty", owner="none"),
            row("row-equal", kind="stdout_equals", value="up"),
            row("row-int", kind="int_at_most", bound=10),
        ),
        reading("row-ok"),
        reading("row-empty", stdout="  \n"),
        reading("row-equal", stdout="up\n"),
        reading("row-int", stdout="7"),
    )
    assert result.passed is True
    assert result.lines() == ()
    assert (result.true_count, result.false_count, result.unread_count) == (4, 0, 0)
    assert {verdict.detail for verdict in result.verdicts} == {"holds"}


@pytest.mark.parametrize(
    ("the_row", "the_reading", "detail"),
    [
        (row(), reading(exit_code=3), "exit 3"),
        (
            row(kind="stdout_empty"),
            reading(stdout="cost-estimator-web\n"),
            "cost-estimator-web",
        ),
        (row(kind="stdout_equals", value="up"), reading(stdout="down"), "read down"),
        (
            row(kind="int_at_most", bound=0),
            reading(stdout="186\n"),
            "read 186, bound 0",
        ),
    ],
    ids=["exit_zero", "stdout_empty", "stdout_equals", "int_at_most"],
)
def test_a_false_row_prints_one_line_naming_ruling_owner_and_fix(
    the_row: ModelRulingRow, the_reading: ModelRulingReading, detail: str
) -> None:
    result = judge((the_row,), the_reading)
    assert result.passed is False
    assert (result.true_count, result.false_count, result.unread_count) == (0, 1, 0)
    (line,) = result.lines()
    assert line.startswith("MISSING ruling-not-running:row-1 | ")
    assert RULING in line
    assert "owner example-lane" in line
    assert f"FALSE: {detail}" in line
    assert line.endswith(" | FIX: fix row-1")


@pytest.mark.parametrize(
    ("the_row", "readings", "detail"),
    [
        (row(), (), "no reading taken"),
        (
            row(),
            (
                reading(
                    status=EnumReadStatus.UNREAD, exit_code=None, detail="timed out"
                ),
            ),
            "read did not answer: timed out",
        ),
        (
            row(),
            (reading(age_seconds=900),),
            "reading is 900 seconds old, bound is 600",
        ),
        (row(), (reading(age_seconds=-30),), "reading is dated after the tick"),
        (
            row(kind="int_at_most", bound=0),
            (reading(stdout="not-a-number"),),
            "answer is not an integer: not-a-number",
        ),
    ],
    ids=["no_reading", "did_not_answer", "stale", "future", "not_an_integer"],
)
def test_error_chain_a_row_that_cannot_be_read_is_unread_and_still_prints_a_line(
    the_row: ModelRulingRow, readings: tuple[ModelRulingReading, ...], detail: str
) -> None:
    result = judge((the_row,), *readings)
    verdict = result.verdicts[0]
    assert verdict.verdict is EnumRulingVerdict.UNREAD
    assert verdict.detail == detail
    assert result.passed is False
    assert (result.true_count, result.false_count, result.unread_count) == (0, 0, 1)
    (line,) = result.lines()
    assert line.startswith("MISSING ruling-not-running:row-1 | ")
    assert f"UNREAD: {detail}" in line


def test_failing_control_an_unread_row_is_never_true() -> None:
    # An UNREAD reading that carries exit 0 and an empty answer would read TRUE on a stdout_empty row
    # if the judge looked at the answer alone; it must read UNREAD.
    result = judge(
        (row(kind="stdout_empty"),),
        reading(
            status=EnumReadStatus.UNREAD, exit_code=0, stdout="", detail="ssh refused"
        ),
    )
    assert result.verdicts[0].verdict is EnumRulingVerdict.UNREAD
    assert result.passed is False


def test_the_newest_reading_of_a_row_is_the_one_judged() -> None:
    result = judge(
        (row(),),
        reading(exit_code=0, age_seconds=300),
        reading(exit_code=4, age_seconds=100),
    )
    assert result.verdicts[0].verdict is EnumRulingVerdict.FALSE


def test_verdicts_follow_row_order_and_mixed_counts_hold() -> None:
    result = judge(
        (row("row-a"), row("row-b", kind="stdout_empty"), row("row-c")),
        reading("row-a"),
        reading("row-b", stdout="x"),
    )
    assert [verdict.row_id for verdict in result.verdicts] == [
        "row-a",
        "row-b",
        "row-c",
    ]
    assert (result.true_count, result.false_count, result.unread_count) == (1, 1, 1)
    assert [line.split(" | ")[0] for line in result.lines()] == [
        "MISSING ruling-not-running:row-b",
        "MISSING ruling-not-running:row-c",
    ]


def test_the_node_judges_the_same_request_as_its_handler_and_a_replay_is_identical() -> (
    None
):
    request = ModelRulingRegisterRequest(
        correlation_id="ruling-1",
        now=NOW,
        policy=ModelRulingRegisterPolicy(
            max_reading_age_seconds=MAX_AGE, rows=(row(),)
        ),
        readings=(reading(exit_code=1),),
    )
    assert NodeRulingRegisterCompute().handle(
        request
    ) == HandlerRulingRegister().handle(request)
    assert HandlerRulingRegister().handle(request) == HandlerRulingRegister().handle(
        request
    )


def test_the_overlay_loader_returns_the_typed_register(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    example = (
        Path(__file__).parents[4]
        / "src/omnimarket/nodes/node_ruling_register_compute/overlay.example.yaml"
    )
    import yaml

    install_node_overlay(
        monkeypatch,
        tmp_path,
        "node_ruling_register_compute",
        yaml.safe_load(example.read_text()),
    )
    policy = load_ruling_register_policy()
    assert len(policy.rows) >= 2
    assert policy.max_reading_age_seconds >= 1


def test_the_overlay_loader_refuses_a_row_with_no_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "node_ruling_register_compute",
        {
            "max_reading_age_seconds": 600,
            "rows": [
                {
                    "row_id": "row-1",
                    "ruling": RULING,
                    "must_be_true": "the example ruling holds",
                    "owner": "none",
                    "fix": "fix row-1",
                }
            ],
        },
    )
    with pytest.raises(NodeOverlayError):
        load_ruling_register_policy()
