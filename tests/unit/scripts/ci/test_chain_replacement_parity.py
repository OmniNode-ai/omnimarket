# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the chain-replacement coverage-parity receipt."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.ci.chain_replacement_parity import (
    DeterminismRun,
    FileCoverage,
    ParityInputs,
    coverage_from_json,
    evaluate,
    junit_case_seconds,
    main,
    parse_args,
)

pytestmark = pytest.mark.unit

_HANDLER = "src/omnimarket/example_handler.py"
_T_CASE = "tests/unit/test_old.py::test_old"
_C_CASE = "tests/chains/test_chain.py::test_chain"
_SHA = "a" * 40


def _good_runs() -> list[DeterminismRun]:
    return [
        DeterminismRun(
            seed=seed,
            passed=True,
            event_lists={_C_CASE: ["Started", "Completed"]},
        )
        for seed in range(1, 21)
    ]


def _input_kwargs() -> dict[str, Any]:
    coverage = {
        _HANDLER: FileCoverage(
            executed_lines=[10, 11, 14],
            executed_branches=[(10, 11), (10, 14)],
        )
    }
    return {
        "flow": "delegation",
        "pinned_sha": _SHA,
        "handler_modules": (_HANDLER,),
        "deleted_cases": (_T_CASE,),
        "chain_cases": (_C_CASE,),
        "mutation_before": {"m1": "killed", "m2": "survived"},
        "mutation_after": {"m1": "killed", "m2": "survived"},
        "mutation_killers": None,
        "coverage_before": coverage,
        "coverage_after": coverage,
        "walker_error_paths_asserted_by_T": ["retry-exhausted"],
        "walker_error_paths_asserted_by_C": ["retry-exhausted", "timeout"],
        "determinism_runs": _good_runs(),
        "case_seconds_T": {_T_CASE: 2.0},
        "case_seconds_C": {_C_CASE: 1.0},
    }


def _inputs(**updates: Any) -> ParityInputs:
    values = _input_kwargs()
    values.update(updates)
    return ParityInputs.model_validate(values)


def _write_inputs(path: Path, inputs: ParityInputs) -> None:
    path.write_text(
        json.dumps(inputs.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )


def test_no_op_deletion_passes_and_cli_exits_zero(tmp_path: Path) -> None:
    inputs = _inputs()
    receipt = evaluate(inputs)

    assert receipt.verdict == "PASS"
    assert {check.status for check in receipt.checks.values()} == {"PASS"}

    inputs_path = tmp_path / "inputs.json"
    receipt_path = tmp_path / "receipt.json"
    _write_inputs(inputs_path, inputs)
    assert (
        main(
            [
                "evaluate",
                "--inputs",
                str(inputs_path),
                "--out",
                str(receipt_path),
            ]
        )
        == 0
    )
    persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert persisted["verdict"] == "PASS"
    assert persisted["schema_version"] == 1


def test_lost_branch_fails_p2_and_cli(tmp_path: Path) -> None:
    after = {
        _HANDLER: FileCoverage(
            executed_lines=[10, 11, 14],
            executed_branches=[(10, 11)],
        )
    }
    inputs = _inputs(coverage_after=after)
    receipt = evaluate(inputs)

    assert receipt.verdict == "FAIL"
    assert receipt.checks["P2"].status == "FAIL"
    assert receipt.checks["P2"].detail["lost_arcs"] == {_HANDLER: [[10, 14]]}

    inputs_path = tmp_path / "inputs.json"
    receipt_path = tmp_path / "receipt.json"
    _write_inputs(inputs_path, inputs)
    assert (
        main(["evaluate", "--inputs", str(inputs_path), "--out", str(receipt_path)])
        == 1
    )


def test_killed_mutant_that_survives_fails_p1() -> None:
    receipt = evaluate(
        _inputs(
            mutation_after={"m1": "survived", "m2": "survived"},
            mutation_killers={"m1": [_T_CASE]},
        )
    )

    assert receipt.checks["P1"].status == "FAIL"
    assert receipt.checks["P1"].detail["escaped_mutants"] == ["m1"]
    assert receipt.checks["P1"].detail["only_T_killed"] == {"m1": "unknown"}


def test_killed_before_mutant_absent_after_fails_p1() -> None:
    receipt = evaluate(_inputs(mutation_after={"m2": "survived"}))

    assert receipt.checks["P1"].status == "FAIL"
    assert receipt.checks["P1"].detail["escaped_mutants"] == ["m1"]


def test_walker_error_path_lost_fails_p3() -> None:
    receipt = evaluate(_inputs(walker_error_paths_asserted_by_C=[]))

    assert receipt.checks["P3"].status == "FAIL"
    assert receipt.checks["P3"].detail["missing_error_paths"] == ["retry-exhausted"]


def test_nineteen_determinism_runs_fail_p4() -> None:
    receipt = evaluate(_inputs(determinism_runs=_good_runs()[:19]))

    assert receipt.checks["P4"].status == "FAIL"
    assert receipt.checks["P4"].detail["run_count"] == 19


def test_changed_event_list_fails_p4() -> None:
    runs = _good_runs()
    runs[-1] = runs[-1].model_copy(
        update={"event_lists": {_C_CASE: ["Started", "Failed"]}}
    )
    receipt = evaluate(_inputs(determinism_runs=runs))

    assert receipt.checks["P4"].status == "FAIL"
    assert _C_CASE in receipt.checks["P4"].detail["non_deterministic_cases"]


def test_duplicate_determinism_seed_fails_p4() -> None:
    runs = _good_runs()
    runs[-1] = runs[-1].model_copy(update={"seed": runs[0].seed})
    receipt = evaluate(_inputs(determinism_runs=runs))

    assert receipt.checks["P4"].status == "FAIL"
    assert receipt.checks["P4"].detail["distinct_seed_count"] == 19


def test_chain_cost_slower_fails_p5() -> None:
    receipt = evaluate(_inputs(case_seconds_C={_C_CASE: 2.1}))

    assert receipt.checks["P5"].status == "FAIL"
    assert receipt.checks["P5"].detail["chain_seconds"] == 2.1


def test_chain_case_without_time_is_missing_p5() -> None:
    receipt = evaluate(_inputs(case_seconds_C={}))

    assert receipt.checks["P5"].status == "MISSING"
    assert receipt.verdict == "FAIL"
    assert receipt.checks["P5"].detail["missing_chain_cases"] == [_C_CASE]


@pytest.mark.parametrize(
    ("field", "check_id"),
    [
        ("mutation_before", "P1"),
        ("mutation_after", "P1"),
        ("coverage_before", "P2"),
        ("coverage_after", "P2"),
        ("walker_error_paths_asserted_by_T", "P3"),
        ("walker_error_paths_asserted_by_C", "P3"),
        ("determinism_runs", "P4"),
        ("case_seconds_T", "P5"),
        ("case_seconds_C", "P5"),
    ],
)
def test_missing_input_fails_closed(
    field: str,
    check_id: str,
    tmp_path: Path,
) -> None:
    receipt = evaluate(_inputs(**{field: None}))
    assert receipt.checks[check_id].status == "MISSING"
    assert receipt.verdict == "FAIL"

    inputs_path = tmp_path / f"{field}.json"
    receipt_path = tmp_path / f"{field}.receipt.json"
    _write_inputs(inputs_path, _inputs(**{field: None}))
    assert (
        main(["evaluate", "--inputs", str(inputs_path), "--out", str(receipt_path)])
        == 1
    )


def test_missing_and_malformed_input_files_exit_one(tmp_path: Path) -> None:
    out = tmp_path / "receipt.json"
    assert (
        main(["evaluate", "--inputs", str(tmp_path / "absent.json"), "--out", str(out)])
        == 1
    )

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not-json", encoding="utf-8")
    assert main(["evaluate", "--inputs", str(malformed), "--out", str(out)]) == 1


def test_coverage_from_json(tmp_path: Path) -> None:
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "meta": {"branch_coverage": True},
                "files": {
                    _HANDLER: {
                        "executed_lines": [10, 11],
                        "executed_branches": [[10, 11], [10, 14]],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert coverage_from_json(report) == {
        _HANDLER: FileCoverage(
            executed_lines=[10, 11],
            executed_branches=[(10, 11), (10, 14)],
        )
    }


def test_junit_case_seconds_maps_classname_to_pytest_node_id(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite>
  <testcase classname="tests.unit.scripts.ci.test_sample" name="test_fast" time="0.125" />
</testsuite></testsuites>
""",
        encoding="utf-8",
    )

    assert junit_case_seconds(report) == {
        "tests/unit/scripts/ci/test_sample.py::test_fast": 0.125
    }


def test_measure_arguments_parse_without_running_measurements(tmp_path: Path) -> None:
    args = parse_args(
        [
            "measure",
            "--flow",
            "sample",
            "--handlers",
            _HANDLER,
            "--deleted",
            str(tmp_path / "deleted.txt"),
            "--chain",
            str(tmp_path / "chain.txt"),
            "--pytest-args=-q -x",
            "--out-dir",
            str(tmp_path / "out"),
        ]
    )

    assert args.command == "measure"
    assert args.handlers == [_HANDLER]
    assert args.pytest_args == "-q -x"


def test_zero_mutants_is_a_missing_measurement_not_a_pass() -> None:
    """An empty mutation run over H must never pass P1 vacuously."""
    receipt = evaluate(_inputs(mutation_before={}, mutation_after={}))
    assert receipt.checks["P1"].status == "MISSING"
    assert receipt.verdict == "FAIL"


def test_junit_case_seconds_resolves_test_classes_against_the_repo(
    tmp_path: Path,
) -> None:
    """A class-based case keeps its class as a node-id segment (h201 finding)."""
    module = tmp_path / "tests" / "unit" / "test_sample.py"
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")
    report = tmp_path / "junit.xml"
    report.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite>
  <testcase classname="tests.unit.test_sample.TestOuter" name="test_a" time="0.5" />
  <testcase classname="tests.unit.test_sample" name="test_b" time="0.25" />
</testsuite></testsuites>
""",
        encoding="utf-8",
    )

    assert junit_case_seconds(report, repo_root=tmp_path) == {
        "tests/unit/test_sample.py::TestOuter::test_a": 0.5,
        "tests/unit/test_sample.py::test_b": 0.25,
    }


def test_a_deleted_test_mutmut_could_not_run_makes_p1_missing() -> None:
    """Kills of a source-reading test are unmeasured, so its deletion is refused."""
    deleted = _inputs().deleted_cases[0]
    receipt = evaluate(_inputs(mutation_unmeasurable_tests=[deleted]))
    assert receipt.checks["P1"].status == "MISSING"
    assert receipt.checks["P1"].detail["unmeasurable_deleted_tests"] == [deleted]
    assert receipt.verdict == "FAIL"


def test_an_unmeasurable_test_that_is_kept_does_not_block_p1() -> None:
    receipt = evaluate(
        _inputs(mutation_unmeasurable_tests=["tests/unit/test_kept.py::test_ast"])
    )
    assert receipt.checks["P1"].status == "PASS"


def test_failed_test_ids_reads_pytest_short_summary_lines() -> None:
    from scripts.ci.chain_replacement_parity import _failed_test_ids

    log = (
        "noise\n"
        "FAILED tests/unit/test_a.py::TestX::test_y - AssertionError: boom\n"
        "FAILED tests/unit/test_b.py::test_z\n"
        "ERROR tests/unit/test_c.py\n"
    )
    assert _failed_test_ids(log) == [
        "tests/unit/test_a.py::TestX::test_y",
        "tests/unit/test_b.py::test_z",
    ]
