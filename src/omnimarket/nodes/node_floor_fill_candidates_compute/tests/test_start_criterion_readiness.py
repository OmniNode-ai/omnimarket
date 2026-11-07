# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_floor_fill_candidates_compute: readiness comes from each task's ``Start when:`` criterion and from nothing an operator says."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_floor_fill_candidates_compute.handlers.handler_floor_fill_candidates import (
    HandlerFloorFillCandidates,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.handlers.handler_start_criterion import (
    parse_start_when,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.models import (
    ModelFloorFillCandidatesRequest,
    ModelFloorFillCandidatesResult,
    ModelFloorFillLine,
    ModelFloorStartCriterion,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.tests.support import (
    LINE,
    item,
    record,
    request,
    usable,
)

pytestmark = pytest.mark.unit

PIECES_FIXTURE = (
    Path(__file__).parent / "fixtures" / "floor-simple-2026-11-07T1232Z.json"
)
NOW = "2026-11-07T12:35:00Z"
RECORD_AT = "2026-11-07T12:34:00Z"


def pieces_text(**piece_overrides: dict[int, dict[str, Any]]) -> str:
    """The served pieces list read at 12:32:35Z, with named pieces rewritten: the injected break of a reading."""
    data = json.loads(PIECES_FIXTURE.read_text())
    for number, fields in piece_overrides.get("pieces", {}).items():
        for piece in data["pieces"]:
            if piece["piece"] == number:
                piece.update(fields)
    return json.dumps(data)


def criterion(task_id: str, start_when: str) -> ModelFloorStartCriterion:
    return ModelFloorStartCriterion(
        line_id=LINE, task_id=task_id, start_when=start_when
    )


def run_tasks(
    tasks: dict[str, tuple[str, str]], **overrides: Any
) -> ModelFloorFillLine:
    """Run one line of tasks ``{id: (floor note, Start when line)}``; every task is a prep lane."""
    items = [item(task, "prep", note) for task, (note, _) in tasks.items()]
    fields: dict[str, Any] = {
        "now": NOW,
        "floor_record": record(items, generated_at=RECORD_AT),
        "pieces_record": pieces_text(),
        "start_criteria": tuple(
            criterion(task, line) for task, (_, line) in tasks.items() if line
        ),
        "ticket_verdicts": tuple(usable(LINE, task, f"TCK-{task}") for task in tasks),
    }
    fields.update(overrides)
    result: ModelFloorFillCandidatesResult = HandlerFloorFillCandidates().handle(
        request(**fields)
    )
    [line] = result.lines
    return line


def ready_ids(line: ModelFloorFillLine) -> list[str]:
    return [c.task_id for c in line.candidates]


# ------------------------------------------------------------------ AC1: readiness from the criterion


def test_start_criterion_readiness_all_prerequisites_true_is_ready() -> None:
    line = run_tasks(
        {
            "A": ("ready", "Start when: piece 1 reads WORKING"),
            "B": (
                "ready",
                "Start when: piece 1 reads WORKING and piece 3 reads WORKING",
            ),
            "C": ("ready", "Start when: none: the node is on dev"),
        }
    )
    assert ready_ids(line) == ["A", "B", "C"]
    assert line.not_ready == ()
    assert line.verdict == "STARTABLE"


def test_start_criterion_readiness_one_unmet_prerequisite_is_not_ready_and_named() -> (
    None
):
    line = run_tasks(
        {
            "A": (
                "ready",
                "Start when: piece 1 reads WORKING and piece 2 reads WORKING",
            ),
            "B": ("ready", "Start when: piece 1 reads WORKING"),
        }
    )
    assert ready_ids(line) == ["B"]
    [refusal] = line.not_ready
    assert refusal.task_id == "A"
    assert refusal.unmet == ("piece 2 WORKING (reads NOT WORKING)",)
    assert refusal.cause == "unmet:piece 2 WORKING (reads NOT WORKING)"


def test_start_criterion_readiness_every_unmet_prerequisite_is_named() -> None:
    line = run_tasks(
        {
            "A": (
                "ready",
                "Start when: piece 2 reads WORKING; piece 4 reads WORKING; piece 7 reads WORKING",
            ),
        }
    )
    [refusal] = line.not_ready
    assert [u.split(" (")[0] for u in refusal.unmet] == [
        "piece 2 WORKING",
        "piece 4 WORKING",
        "piece 7 WORKING",
    ]
    assert refusal.unmet[2] == "piece 7 WORKING (reads UNKNOWN)"


def test_start_criterion_readiness_flips_with_the_reading_the_failing_control() -> None:
    task = {"A": ("ready", "Start when: piece 2 reads WORKING")}
    assert ready_ids(run_tasks(task)) == []
    working = pieces_text(pieces={2: {"reading_state": "WORKING"}})
    assert ready_ids(run_tasks(task, pieces_record=working)) == ["A"]
    broken = pieces_text(
        pieces={2: {"reading_state": "WORKING", "reading_age_s": 7201}}
    )
    [refusal] = run_tasks(task, pieces_record=broken).not_ready
    assert refusal.unmet == ("piece 2 WORKING (reading is old)",)


@pytest.mark.parametrize(
    ("pieces", "cause"),
    [
        (None, "absent"),
        ("not json", "malformed"),
        ('{"pieces": []}', "malformed"),
    ],
)
def test_start_criterion_readiness_unread_pieces_list_is_unmet_not_empty(
    pieces: str | None, cause: str
) -> None:
    line = run_tasks(
        {"A": ("ready", "Start when: piece 1 reads WORKING")}, pieces_record=pieces
    )
    [refusal] = line.not_ready
    assert refusal.unmet == (f"piece 1 WORKING (pieces list unread ({cause}))",)


def test_start_criterion_readiness_old_pieces_list_is_unmet() -> None:
    line = run_tasks(
        {"A": ("ready", "Start when: piece 1 reads WORKING")},
        now="2026-11-07T12:48:36Z",
        floor_record=record([item("A")], generated_at="2026-11-07T12:48:00Z"),
    )
    [refusal] = line.not_ready
    assert refusal.unmet == ("piece 1 WORKING (pieces list unread (old))",)


def test_start_criterion_readiness_piece_missing_from_the_list_is_unmet() -> None:
    line = run_tasks({"A": ("ready", "Start when: piece 99 reads WORKING")})
    assert line.not_ready[0].unmet == (
        "piece 99 WORKING (piece not on the pieces list)",
    )


def test_start_criterion_readiness_merged_reference_reads_the_merged_list() -> None:
    task = {"A": ("ready", "Start when: omnimarket#4922 is merged and TCK-7 merged")}
    [refusal] = run_tasks(task).not_ready
    assert refusal.unmet == (
        "omnimarket#4922 merged (not merged)",
        "TCK-7 merged (not merged)",
    )
    [refusal] = run_tasks(task, merged_refs=("omnimarket#4922",)).not_ready
    assert refusal.unmet == ("TCK-7 merged (not merged)",)
    met = run_tasks(task, merged_refs=("OmniNode-ai/omnimarket#4922", "tck-7"))
    assert ready_ids(met) == ["A"]


def test_start_criterion_readiness_m0_pass_gates_stop_admission_and_holder_work() -> (
    None
):
    task = {"A": ("ready", "Start when: M0 PASS and piece 1 reads WORKING")}
    for verdict in ("UNREAD", "FAIL"):
        [refusal] = run_tasks(task, m0_verdict=verdict).not_ready
        assert refusal.unmet == (f"M0 PASS (M0 reads {verdict})",)
    assert ready_ids(run_tasks(task, m0_verdict="PASS")) == ["A"]


def test_start_criterion_readiness_task_without_a_criterion_is_not_ready() -> None:
    line = run_tasks({"A": ("ready", "")})
    assert ready_ids(line) == []
    assert line.not_ready[0].cause == "no-start-criterion"
    assert line.verdict == "NO-STARTABLE"


def test_start_criterion_readiness_occupancy_still_holds_a_ready_criterion() -> None:
    line = run_tasks(
        {"A": ("ready", "Start when: none")},
        floor_record=record(
            [item("A", "prep", "ready", prs=("example/repo#9",))],
            generated_at=RECORD_AT,
        ),
    )
    assert line.not_ready[0].cause == "open-pr:example/repo#9"


def test_start_criterion_readiness_ready_task_still_needs_a_ticket_or_payload() -> None:
    line = run_tasks({"A": ("ready", "Start when: none")}, ticket_verdicts=())
    assert line.verdict == "UNTICKETABLE"
    assert line.untickettable[0].cause == "plan-unread"


# ------------------------------------------------------------------ AC2: no operator go


def _field_names(model: type[Any], seen: set[type[Any]] | None = None) -> set[str]:
    seen = seen if seen is not None else set()
    seen.add(model)
    names: set[str] = set()
    for name, field in model.model_fields.items():
        names.add(name)
        for arg in (field.annotation, *getattr(field.annotation, "__args__", ())):
            if hasattr(arg, "model_fields") and arg not in seen:
                names |= _field_names(arg, seen)
    return names


def test_no_operator_go_field_in_the_request() -> None:
    names = _field_names(ModelFloorFillCandidatesRequest)
    assert {"start_criteria", "pieces_record", "merged_refs", "m0_verdict"} <= names
    for name in names:
        assert not {
            "go",
            "operator",
            "approve",
            "approved",
            "release",
            "released",
        } & set(name.split("_")), name


@pytest.mark.parametrize(
    "field", ["operator_go", "go", "approved", "released", "operator_says"]
)
def test_no_operator_go_field_is_refused_when_carried(field: str) -> None:
    fields: dict[str, Any] = {
        "now": NOW,
        "floor_record": None,
        "max_age_minutes": 30,
        "max_fill_lanes_per_line": 2,
        "sent_window_minutes": 60,
        field: True,
    }
    with pytest.raises(ValidationError):
        ModelFloorFillCandidatesRequest(**fields)


@pytest.mark.parametrize(
    "note", ["ready", "go", "operator go", "operator says go", "released by operator"]
)
def test_no_operator_go_task_that_carried_one_is_not_ready_while_its_criterion_is_unmet(
    note: str,
) -> None:
    line = run_tasks({"A": (note, "Start when: piece 2 reads WORKING")})
    assert ready_ids(line) == []
    assert line.not_ready[0].unmet == ("piece 2 WORKING (reads NOT WORKING)",)


@pytest.mark.parametrize(
    "note", ["waits for the operator go", "operator hold", "not-ready", "go"]
)
def test_no_operator_go_task_reads_ready_by_its_criterion_whatever_the_floor_note_says(
    note: str,
) -> None:
    items = [item("A", "wait", note)]
    line = run_tasks(
        {"A": (note, "Start when: piece 1 reads WORKING")},
        floor_record=record(items, generated_at=RECORD_AT),
    )
    assert ready_ids(line) == ["A"]


@pytest.mark.parametrize(
    "line",
    [
        "Start when: operator go",
        "Start when: the operator says go",
        "Start when: piece 1 reads WORKING and the operator says go",
        "Start when: approved by the operator",
    ],
)
def test_no_operator_go_in_a_criterion_line_is_unparseable_not_ready(line: str) -> None:
    refused = run_tasks({"A": ("ready", line)})
    assert ready_ids(refused) == []
    assert refused.not_ready[0].cause.startswith("start-when-unparseable:")


# ------------------------------------------------------------------ AC3: the parser


@pytest.mark.parametrize(
    ("text", "labels"),
    [
        ("Start when: none", []),
        ("none", []),
        (
            "Start when: none: node_floor_fill_candidates_compute is on omnimarket dev (omnimarket#4922) and the floor pieces list is served",
            [],
        ),
        ("Start when: piece 1 reads WORKING", ["piece 1 WORKING"]),
        ("**Start when:** Piece 3 is WORKING.", ["piece 3 WORKING"]),
        ("> Start when: piece 2 WORKING", ["piece 2 WORKING"]),
        (
            "Start when: pieces 1, 3 and 5 read WORKING",
            ["piece 1 WORKING", "piece 3 WORKING", "piece 5 WORKING"],
        ),
        (
            "Start when: piece 6 reads WORKING and omnimarket#4922 is merged",
            ["piece 6 WORKING", "omnimarket#4922 merged"],
        ),
        (
            "Start when: OmniNode-ai/omnimarket#5243 merged; M0 PASS",
            ["omnimarket#5243 merged", "M0 PASS"],
        ),
        ("Start when: TCK-12 merged", ["TCK-12 merged"]),
        ("Start when: M0 has a PASS verdict", ["M0 PASS"]),
        (
            "Start when: piece 3 reads WORKING (golden and error read) and TCK-9 merged (the node)",
            ["piece 3 WORKING", "TCK-9 merged"],
        ),
        (
            "start when: piece 1 reads WORKING, TCK-3 is merged & M0 PASS",
            ["piece 1 WORKING", "TCK-3 merged", "M0 PASS"],
        ),
    ],
)
def test_parse_start_when_line_reads_the_prerequisites(
    text: str, labels: list[str]
) -> None:
    parse = parse_start_when(text)
    assert parse.unparseable_cause == ""
    assert [p.label() for p in parse.prerequisites] == labels


@pytest.mark.parametrize(
    ("text", "cause"),
    [
        ("", "empty line"),
        ("Start when:", "empty line"),
        ("Start when:   ", "empty line"),
        ("Start when: soon", "cannot read 'soon'"),
        (
            "Start when: piece 2 reads NOT WORKING",
            "cannot read 'piece 2 reads NOT WORKING'",
        ),
        (
            "Start when: piece 1 reads WORKING and the stars align",
            "cannot read 'the stars align'",
        ),
        ("Start when: piece reads WORKING", "cannot read 'piece reads WORKING'"),
        ("Start when: nonexistent", "cannot read 'nonexistent'"),
        ("Start when: .", "no prerequisite named"),
    ],
)
def test_parse_start_when_line_unparseable_names_the_cause(
    text: str, cause: str
) -> None:
    parse = parse_start_when(text)
    assert parse.prerequisites == ()
    assert parse.unparseable_cause == cause


def test_parse_start_when_line_unparseable_task_is_not_ready_with_the_cause() -> None:
    line = run_tasks({"A": ("ready", "Start when: soon")})
    assert ready_ids(line) == []
    assert line.not_ready[0].cause == "start-when-unparseable:cannot read 'soon'"
