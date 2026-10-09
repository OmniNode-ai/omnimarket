# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The full-suite shard planner and its CI gate (OMN-19684).

Live PR runs after the duration-cache fix spread 1.75-2.10x. The shards now run
only the test files `merge_test_durations.py plan` assigns them from the
committed `config/test_file_durations.json`; these tests pin the planner's
exact-once assignment, its determinism, the committed record's fitness and the
CI job that proves the plan partitions what pytest collects.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "merge_test_durations.py"
RECORD = REPO_ROOT / "config" / "test_file_durations.json"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
FULL_SUITE_SPLITS = 20
# A balanced plan leaves headroom below the 1.5x acceptance target for runner noise.
MAX_PLANNED_SPREAD = 1.1


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("merge_test_durations", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


planner = _load_script()


def _tree(root: Path, names: list[str]) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")


def test_discovery_follows_pytest_file_and_directory_rules(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        [
            "tests/test_a.py",
            "tests/unit/b_test.py",
            "tests/unit/conftest.py",
            "tests/unit/helper.py",
            "tests/unit/test_data.json",
            "tests/.hidden/test_c.py",
            "tests/build/test_d.py",
            "tests/node_modules/test_e.py",
            "tests/deep/er/test_f.py",
        ],
    )
    assert planner.discover_test_files(tmp_path) == [
        "tests/deep/er/test_f.py",
        "tests/test_a.py",
        "tests/unit/b_test.py",
    ]


def test_every_file_lands_in_exactly_one_shard() -> None:
    weights = {
        f"tests/test_{index:03d}.py": float(index % 7 + 1) for index in range(97)
    }
    shards = planner.plan_shards(weights, 20)
    assert len(shards) == 20
    flat = [name for shard in shards for name in shard]
    assert sorted(flat) == sorted(weights)
    assert len(flat) == len(set(flat))
    assert planner.verify_partition(shards, weights) == []


def test_unrecorded_file_is_assigned_once_and_takes_the_median() -> None:
    record = {"tests/a.py": 1.0, "tests/b.py": 3.0, "tests/c.py": 9.0}
    weights, unrecorded = planner.file_weights(
        ["tests/a.py", "tests/b.py", "tests/c.py", "tests/new.py"], record
    )
    assert unrecorded == ["tests/new.py"]
    assert weights["tests/new.py"] == 3.0
    shards = planner.plan_shards(weights, 3)
    assert sum(name == "tests/new.py" for shard in shards for name in shard) == 1


def test_plan_is_deterministic_for_the_same_inputs() -> None:
    weights = {f"tests/t{index}.py": 2.0 for index in range(40)}
    first = planner.plan_shards(weights, 7)
    assert planner.plan_shards(dict(reversed(list(weights.items()))), 7) == first


def test_plan_balances_longest_first() -> None:
    weights = {"tests/big.py": 10.0, **{f"tests/s{i}.py": 1.0 for i in range(10)}}
    shards = planner.plan_shards(weights, 2)
    assert planner.shard_loads(weights, shards) == [10.0, 10.0]


def test_verify_names_a_collected_file_that_is_in_no_shard() -> None:
    shards = [["tests/a.py"], ["tests/b.py"]]
    problems = planner.verify_partition(
        shards, {"tests/a.py", "tests/b.py", "tests/c.py"}
    )
    assert problems == ["tests/c.py is collected by pytest but in no shard"]
    duplicate = planner.verify_partition(
        [["tests/a.py"], ["tests/a.py"]], {"tests/a.py"}
    )
    assert duplicate == ["tests/a.py is in shard 1 and shard 2"]


def test_collected_files_reads_pytest_node_ids_only() -> None:
    output = "\n".join(
        [
            "tests/a/test_x.py::test_one",
            "tests/a/test_x.py::TestK::test_two[param-1]",
            "tests/b/test_y.py::test_three",
            "",
            "=============== warnings summary ===============",
            "  /abs/path.py:1: DeprecationWarning: x::y",
            "ERROR tests/c/test_z.py::test_four",
            "40306/40320 tests collected (14 deselected) in 60.96s",
        ]
    )
    assert planner.collected_files(output) == {
        "tests/a/test_x.py",
        "tests/b/test_y.py",
    }


def test_record_sums_per_test_seconds_into_files_and_means_over_runs() -> None:
    run_one = planner.file_durations(
        {"tests/a.py::t1": 1.0, "tests/a.py::C::t2": 2.0, "tests/b.py::t": 4.0}
    )
    run_two = planner.file_durations({"tests/a.py::t1": 5.0})
    assert run_one == {"tests/a.py": 3.0, "tests/b.py": 4.0}
    assert planner.mean_file_durations([run_one, run_two]) == {
        "tests/a.py": 4.0,
        "tests/b.py": 4.0,
    }


def test_committed_record_balances_the_real_tree_within_target() -> None:
    record: dict[str, float] = json.loads(RECORD.read_text(encoding="utf-8"))
    assert record, "config/test_file_durations.json is empty"
    assert all(isinstance(v, (int, float)) and v >= 0 for v in record.values())
    files = planner.discover_test_files(REPO_ROOT)
    weights, _ = planner.file_weights(files, record)
    shards = planner.plan_shards(weights, FULL_SUITE_SPLITS)
    assert all(shards), "a full-suite shard with no files would run the whole tree"
    loads = planner.shard_loads(weights, shards)
    spread = max(loads) / min(loads)
    assert spread <= MAX_PLANNED_SPREAD, (
        f"planned shard spread {spread:.2f}x over {MAX_PLANNED_SPREAD}x -- one file "
        "outweighs a shard's share; split it or refresh the record"
    )
    heaviest = max(weights.values())
    assert heaviest <= sum(loads) / FULL_SUITE_SPLITS, (
        "a single test file is heavier than a shard's share, so file-level "
        "planning cannot balance it"
    )


def test_plan_command_prints_one_shards_files(tmp_path: Path) -> None:
    _tree(tmp_path, [f"tests/test_{name}.py" for name in "abcd"])
    record = tmp_path / "record.json"
    record.write_text(json.dumps({"tests/test_a.py": 5.0, "tests/test_b.py": 1.0}))
    printed: list[str] = []
    for group in (1, 2):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "plan",
                "--splits",
                "2",
                "--group",
                str(group),
                "--record",
                str(record),
                "--root",
                str(tmp_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        printed.extend(result.stdout.split())
    assert sorted(printed) == [f"tests/test_{name}.py" for name in "abcd"]


def test_plan_command_refuses_an_empty_shard(tmp_path: Path) -> None:
    _tree(tmp_path, ["tests/test_only.py"])
    record = tmp_path / "record.json"
    record.write_text("{}")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "plan",
            "--splits",
            "2",
            "--group",
            "2",
            "--record",
            str(record),
            "--root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "no test files" in result.stderr


def test_verify_command_fails_when_pytest_collects_a_file_outside_the_plan(
    tmp_path: Path,
) -> None:
    _tree(tmp_path, ["tests/test_a.py"])
    record = tmp_path / "record.json"
    record.write_text("{}")
    collected = tmp_path / "collected.txt"
    collected.write_text("tests/test_a.py::test_x\ntests/odd/check_b.py::test_y\n")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "verify",
            "--splits",
            "1",
            "--record",
            str(record),
            "--root",
            str(tmp_path),
            "--collected",
            str(collected),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert (
        "tests/odd/check_b.py is collected by pytest but in no shard" in result.stdout
    )


def _workflow() -> dict[str, Any]:
    document = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_typecheck_job_proves_the_plan_partitions_the_collected_tree() -> None:
    steps = _workflow()["jobs"]["typecheck"]["steps"]
    runs = [str(step.get("run", "")) for step in steps]
    collect = next(i for i, run in enumerate(runs) if "--collect-only" in run)
    verify = next(
        i for i, run in enumerate(runs) if "merge_test_durations.py verify" in run
    )
    assert collect < verify
    assert 'tests/ --collect-only -q -m "not kafka"' in runs[collect]
    assert "--collected collected.txt" in runs[verify]
    # An empty verify would pass a plan against nothing: it reads the collect output.
    assert "> collected.txt" in runs[collect]


def test_verified_split_count_is_the_full_suite_split_count() -> None:
    from scripts.ci import detect_test_paths
    from scripts.ci.test_selection_models import EnumFullSuiteReason

    full = detect_test_paths._full_suite(EnumFullSuiteReason.MAIN_BRANCH)
    assert full.split_count == FULL_SUITE_SPLITS
    runs = [
        str(step.get("run", "")) for step in _workflow()["jobs"]["typecheck"]["steps"]
    ]
    verify = next(run for run in runs if "merge_test_durations.py verify" in run)
    assert f"--splits {FULL_SUITE_SPLITS} " in verify
