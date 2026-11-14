# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The report importer: a lab experiment report's result table read into typed arms, never a hand-typed number.

The fixtures are verbatim excerpts (headings, tables, paired-test lines, verdict lines) of three real lab reports. The golden reads
check numbers the reports state; each error is a named injected break (a row whose correct exceeds n, a bound written as p<, a
column that counts items, a malformed listing header) and each turns the reading to a named problem, never a number and never a raise.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_floor_delegation_history_effect.handlers.adapter_report_tables import (
    parse_report,
    split_remote_listing,
)

from .support import FIXTURES, listing

pytestmark = pytest.mark.unit

MARKERS = ("u_items.json", "task set")


def _parse(name: str, report_id: str):
    text = (FIXTURES / "reports" / name).read_text(encoding="utf-8")
    return parse_report(
        text,
        report_id=report_id,
        source=f"host:{name}",
        date="2026-11-13T20:45:00Z",
        date_source="filename",
        hash_markers=MARKERS,
    )


def test_golden_triagepow_table_is_read_as_the_report_states_it() -> None:
    experiments, problems = _parse(
        "exp-triagepow-6800-2026-11-13T2045Z.md", "exp-triagepow-6800"
    )
    assert problems == ()
    assert [e.id for e in experiments] == ["exp-triagepow-6800"]
    exp = experiments[0]
    assert exp.question.startswith("triage kinds with enough power")
    by = {a.label: a for a in exp.arms}
    assert (by["local_zero"].correct, by["local_zero"].n) == (
        114,
        156,
    )  # 114/156 = 0.731 in the report
    assert (by["local_fewdet"].correct, by["local_fewdet"].n) == (130, 156)
    assert (by["local_zero"].p50_latency_s, by["local_zero"].gpu_seconds) == (
        0.58,
        89.8,
    )
    assert (
        exp.task_set_hash
        == "de4a24de6aa9eef51a3119b0af3d8cc93a1c42a2d6e260fa6de4c60b4838a85b"
    )
    assert exp.verdict is not None
    assert exp.verdict.startswith("Result: product stays current")
    assert len(exp.paired) == 10
    first = exp.paired[0]
    assert (
        first.test,
        first.arm_a,
        first.arm_b,
        first.only_a_right,
        first.only_b_right,
        first.p_value,
    ) == (
        "exact McNemar",
        "local_zero",
        "local_fewdet",
        7,
        23,
        0.0052,
    )


def test_golden_a_report_with_several_tables_gives_one_experiment_per_table_with_its_own_pairs() -> (
    None
):
    experiments, problems = _parse(
        "exp-glmlocal-6799-2026-11-13T1730Z.md", "exp-glmlocal-6799"
    )
    assert problems == ()
    assert [e.id for e in experiments] == [
        "exp-glmlocal-6799",
        "exp-glmlocal-6799#2",
        "exp-glmlocal-6799#3",
    ]
    assert [len(e.arms) for e in experiments] == [6, 3, 3]
    assert {a.label for a in experiments[1].arms} == {"local201", "local202", "glm"}
    assert all(e.paired for e in experiments)
    # a per-kind table and a calibration table in the same reports are not result tables: their first column is not "arm"
    assert all(a.label != "base-red" for e in experiments for a in e.arms)


def test_golden_the_remote_listing_round_trips_the_files() -> None:
    files, problems = split_remote_listing(
        listing(("a.md", 5, "# a\n\nline"), ("exp-b/result.md", 9, "# b"))
    )
    assert problems == ()
    assert [(f.name, f.mtime_epoch) for f in files] == [
        ("a.md", 5),
        ("exp-b/result.md", 9),
    ]
    assert files[0].text.startswith("# a")


def test_error_a_row_whose_correct_exceeds_n_is_skipped_with_a_problem() -> None:
    text = "# exp-x: q\n\n| arm | correct |\n|---|---|\n| a | 5/3 |\n| b | 2/4 |\n"
    experiments, problems = parse_report(
        text,
        report_id="exp-x",
        source="h:x",
        date="2026-11-13T00:00:00Z",
        date_source="filename",
        hash_markers=(),
    )
    assert [(a.label, a.correct, a.n) for a in experiments[0].arms] == [("b", 2, 4)]
    assert any("5/3" in p for p in problems)


def test_error_a_bound_written_as_p_less_than_is_not_turned_into_a_number() -> None:
    text = (
        "# exp-x: q\n\n| arm | correct |\n|---|---|\n| a | 2/4 |\n| b | 4/4 |\n\n"
        "- McNemar a vs b: only-a right 0, only-b right 2, p<0.0001\n"
        "- McNemar b vs a: only-b right 2, only-a right 0, p=0.5\n"
    )
    experiments, problems = parse_report(
        text,
        report_id="exp-x",
        source="h:x",
        date="2026-11-13T00:00:00Z",
        date_source="filename",
        hash_markers=(),
    )
    assert [p.p_value for p in experiments[0].paired] == [0.5]
    assert any("bound" in p for p in problems)


def test_error_a_column_that_counts_items_is_not_the_result_column() -> None:
    text = "# exp-x: q\n\n| arm | n (pos/neg) | correct |\n|---|---|---|\n| a | 90/50 | 30/60 |\n| b | 90/50 | 45/60 |\n"
    experiments, _ = parse_report(
        text,
        report_id="exp-x",
        source="h:x",
        date="2026-11-13T00:00:00Z",
        date_source="filename",
        hash_markers=(),
    )
    assert [(a.correct, a.n) for a in experiments[0].arms] == [(30, 60), (45, 60)]


def test_error_a_table_that_is_not_an_arm_table_gives_no_experiment() -> None:
    text = "# exp-x: q\n\n| kind | correct |\n|---|---|\n| a | 2/4 |\n"
    experiments, problems = parse_report(
        text,
        report_id="exp-x",
        source="h:x",
        date="2026-11-13T00:00:00Z",
        date_source="filename",
        hash_markers=(),
    )
    assert experiments == ()
    assert problems == ()


def test_error_a_baseline_row_is_the_baseline_and_no_arm() -> None:
    text = "# exp-x: q\n\n| arm | correct |\n|---|---|\n| majority answer (product) | 30/156 |\n| a | 2/4 |\n"
    experiments, _ = parse_report(
        text,
        report_id="exp-x",
        source="h:x",
        date="2026-11-13T00:00:00Z",
        date_source="filename",
        hash_markers=(),
    )
    assert experiments[0].baseline is not None
    assert (experiments[0].baseline.correct, experiments[0].baseline.n) == (30, 156)
    assert [a.label for a in experiments[0].arms] == ["a"]


def test_error_a_report_with_no_verdict_and_no_hash_names_none_and_never_invents_one() -> (
    None
):
    text = "# exp-x: q\n\n| arm | correct |\n|---|---|\n| a | 2/4 |\n"
    experiments, _ = parse_report(
        text,
        report_id="exp-x",
        source="h:x",
        date="2026-11-13T00:00:00Z",
        date_source="filename",
        hash_markers=MARKERS,
    )
    assert experiments[0].verdict is None
    assert experiments[0].task_set_hash is None


def test_error_a_malformed_listing_header_is_a_problem_and_skipped() -> None:
    files, problems = split_remote_listing(
        "@@FILE only-a-name\ntext\n@@FILE ok.md 7\nbody\n"
    )
    assert [f.name for f in files] == ["ok.md"]
    assert len(problems) == 1


def test_error_a_garbled_report_gives_no_experiment_and_never_raises() -> None:
    experiments, _ = parse_report(
        "|||\n| arm |\n|--|\n| a | 9/0 |\n",
        report_id="exp-y",
        source="h:y",
        date="d",
        date_source="filename",
        hash_markers=(),
    )
    assert experiments == ()
