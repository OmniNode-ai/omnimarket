# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Prove parity when replacing individual tests with chain tests.

The pure :func:`evaluate` layer accepts already-collected measurements and is
the authority for the P1--P5 receipt.  The ``measure`` subcommand is a thin,
lab-only collector.  It runs coverage and mutmut in isolated runs, checks
determinism, records pytest JUnit timings, writes ``inputs.json``, and then
calls the same pure evaluator.

Determinism intentionally does not depend on pytest-randomly.  For seeds
1..20, the collector sets ``PYTHONHASHSEED`` and shuffles the explicit chain
node ids with ``random.Random(seed)`` before passing them to pytest.

Chain cases that support event-order verification must append one JSON object
per case to the path named by ``CHAIN_EVENT_LOG``::

    {"case_id": "tests/chains/test_x.py::test_y",
     "event_types": ["Started", "Completed"]}

A chain case missing from a run's log is a missing P4 measurement.  JUnit
``classname`` values are mapped to pytest ids by resolving the longest dotted
prefix that names a ``.py`` file under the repository; the remaining parts are
test classes.  ``tests.unit.test_x.TestY`` plus ``test_z`` becomes
``tests/unit/test_x.py::TestY::test_z``.

The mutmut collector requires mutmut 3 from the ``parity`` dependency group.
It copies the repository to scratch directories and appends a temporary
``[tool.mutmut]`` section there; it never edits this worktree's configuration.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

CheckStatus = Literal["PASS", "FAIL", "MISSING"]
Verdict = Literal["PASS", "FAIL"]
CheckId = Literal["P1", "P2", "P3", "P4", "P5"]
MutmutStatus = Literal[
    "killed",
    "survived",
    "timeout",
    "suspicious",
    "no tests",
    "skipped",
    "segfault",
    "not checked",
]

_KILLED_STATUSES: frozenset[MutmutStatus] = frozenset({"killed", "timeout"})
_ALL_CHECK_IDS: frozenset[CheckId] = frozenset({"P1", "P2", "P3", "P4", "P5"})
_MUTMUT_STATUSES = (
    "killed",
    "survived",
    "timeout",
    "suspicious",
    "no tests",
    "skipped",
    "segfault",
    "not checked",
)


class FileCoverage(BaseModel):
    """Executed statements and branch arcs for one source file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    executed_lines: list[int]
    executed_branches: list[tuple[int, int]]


class DeterminismRun(BaseModel):
    """One explicitly seeded execution of all chain cases."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    seed: int
    passed: bool
    event_lists: dict[str, list[str]]


class ParityInputs(BaseModel):
    """Identity and measurements consumed by the pure parity evaluator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    flow: str = Field(min_length=1)
    pinned_sha: str = Field(pattern=r"^[0-9a-fA-F]{40}$")
    handler_modules: tuple[str, ...] = Field(min_length=1)
    deleted_cases: tuple[str, ...] = Field(min_length=1)
    chain_cases: tuple[str, ...] = Field(min_length=1)
    mutation_before: dict[str, MutmutStatus] | None
    mutation_after: dict[str, MutmutStatus] | None
    mutation_killers: dict[str, list[str]] | None = None
    mutation_unmeasurable_tests: list[str] = Field(default_factory=list)
    coverage_before: dict[str, FileCoverage] | None
    coverage_after: dict[str, FileCoverage] | None
    walker_error_paths_asserted_by_T: list[str] | None  # noqa: N815 - specified schema
    walker_error_paths_asserted_by_C: list[str] | None  # noqa: N815 - specified schema
    determinism_runs: list[DeterminismRun] | None
    case_seconds_T: dict[str, float] | None  # noqa: N815 - specified schema
    case_seconds_C: dict[str, float] | None  # noqa: N815 - specified schema

    @field_validator("handler_modules")
    @classmethod
    def _validate_handler_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
                raise ValueError(
                    f"handler_modules must be repo-relative .py paths: {value!r}"
                )
        return values


class CheckResult(BaseModel):
    """One parity-check result and its machine-readable evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: CheckStatus
    detail: dict[str, Any]


class ParityReceipt(BaseModel):
    """Versioned P1--P5 parity verdict."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    flow: str
    pinned_sha: str
    handler_modules: tuple[str, ...]
    deleted_cases: tuple[str, ...]
    chain_cases: tuple[str, ...]
    checks: dict[CheckId, CheckResult]
    verdict: Verdict
    generated_by: Literal["scripts/ci/chain_replacement_parity.py"] = (
        "scripts/ci/chain_replacement_parity.py"
    )
    tool_version: Literal[1] = 1

    @field_validator("checks")
    @classmethod
    def _require_all_checks(
        cls, checks: dict[CheckId, CheckResult]
    ) -> dict[CheckId, CheckResult]:
        if frozenset(checks) != _ALL_CHECK_IDS:
            raise ValueError("checks must contain exactly P1, P2, P3, P4, and P5")
        return checks


def _p1_mutation(inputs: ParityInputs) -> CheckResult:
    missing = [
        name
        for name, value in (
            ("mutation_before", inputs.mutation_before),
            ("mutation_after", inputs.mutation_after),
        )
        if value is None
    ]
    if missing:
        return CheckResult(status="MISSING", detail={"missing": missing})

    before = cast(dict[str, MutmutStatus], inputs.mutation_before)
    after = cast(dict[str, MutmutStatus], inputs.mutation_after)
    unmeasured_deletions = sorted(
        set(inputs.mutation_unmeasurable_tests) & set(inputs.deleted_cases)
    )
    if unmeasured_deletions:
        # A deleted test that mutation could not run has kills nobody measured.
        return CheckResult(
            status="MISSING",
            detail={
                "missing": ["mutation kills of deleted tests mutmut could not run"],
                "unmeasurable_deleted_tests": unmeasured_deletions,
            },
        )
    if not before:
        # Zero mutants over H is not a measurement: mutmut found nothing to
        # mutate or the run collected nothing. It never passes vacuously.
        return CheckResult(
            status="MISSING", detail={"missing": ["mutation_before (no mutants)"]}
        )
    killed_before = {
        key for key, status in before.items() if status in _KILLED_STATUSES
    }
    killed_after = {key for key, status in after.items() if status in _KILLED_STATUSES}
    escaped = sorted(killed_before - killed_after)

    only_t_killed: dict[str, str] = {}
    if inputs.mutation_killers is not None:
        deleted = set(inputs.deleted_cases)
        chain = set(inputs.chain_cases)
        for mutant_id in sorted(killed_before):
            killers = inputs.mutation_killers.get(mutant_id, [])
            if killers and set(killers) <= deleted:
                only_t_killed[mutant_id] = next(
                    (case_id for case_id in killers if case_id in chain), "unknown"
                )

    return CheckResult(
        status="PASS" if not escaped else "FAIL",
        detail={
            "killed_before_count": len(killed_before),
            "killed_after_count": len(killed_after),
            "escaped_mutants": escaped,
            "only_T_killed": only_t_killed,
        },
    )


def _p2_coverage(inputs: ParityInputs) -> CheckResult:
    missing_inputs = [
        name
        for name, value in (
            ("coverage_before", inputs.coverage_before),
            ("coverage_after", inputs.coverage_after),
        )
        if value is None
    ]
    if missing_inputs:
        return CheckResult(status="MISSING", detail={"missing": missing_inputs})

    before = cast(dict[str, FileCoverage], inputs.coverage_before)
    after = cast(dict[str, FileCoverage], inputs.coverage_after)
    missing_files: dict[str, list[str]] = {}
    lost_lines: dict[str, list[int]] = {}
    lost_arcs: dict[str, list[list[int]]] = {}

    for handler in inputs.handler_modules:
        absent_from = [
            report_name
            for report_name, report in (("before", before), ("after", after))
            if handler not in report
        ]
        if absent_from:
            missing_files[handler] = absent_from
            continue
        before_file = before[handler]
        after_file = after[handler]
        lines = sorted(set(before_file.executed_lines) - set(after_file.executed_lines))
        arcs = sorted(
            set(before_file.executed_branches) - set(after_file.executed_branches)
        )
        if lines:
            lost_lines[handler] = lines
        if arcs:
            lost_arcs[handler] = [[source, destination] for source, destination in arcs]

    detail: dict[str, Any] = {
        "missing_files": missing_files,
        "lost_lines": lost_lines,
        "lost_arcs": lost_arcs,
    }
    if missing_files:
        return CheckResult(status="MISSING", detail=detail)
    return CheckResult(
        status="FAIL" if lost_lines or lost_arcs else "PASS",
        detail=detail,
    )


def _p3_walker(inputs: ParityInputs) -> CheckResult:
    missing = [
        name
        for name, value in (
            (
                "walker_error_paths_asserted_by_T",
                inputs.walker_error_paths_asserted_by_T,
            ),
            (
                "walker_error_paths_asserted_by_C",
                inputs.walker_error_paths_asserted_by_C,
            ),
        )
        if value is None
    ]
    if missing:
        return CheckResult(status="MISSING", detail={"missing": missing})

    asserted_t = set(cast(list[str], inputs.walker_error_paths_asserted_by_T))
    asserted_c = set(cast(list[str], inputs.walker_error_paths_asserted_by_C))
    lost = sorted(asserted_t - asserted_c)
    return CheckResult(
        status="PASS" if not lost else "FAIL",
        detail={
            "missing_error_paths": lost,
            "asserted_by_T_count": len(asserted_t),
            "asserted_by_C_count": len(asserted_c),
        },
    )


def _p4_determinism(inputs: ParityInputs) -> CheckResult:
    if inputs.determinism_runs is None:
        return CheckResult(status="MISSING", detail={"missing": ["determinism_runs"]})

    runs = inputs.determinism_runs
    required_cases = set(inputs.chain_cases)
    missing_event_logs: dict[str, list[int]] = {}
    for case_id in sorted(required_cases):
        absent_seeds = sorted(
            run.seed for run in runs if case_id not in run.event_lists
        )
        if absent_seeds:
            missing_event_logs[case_id] = absent_seeds

    distinct_seeds = {run.seed for run in runs}
    failed_seeds = sorted(run.seed for run in runs if not run.passed)
    all_cases = sorted(
        required_cases.union(*(set(run.event_lists) for run in runs))
        if runs
        else required_cases
    )
    non_deterministic_cases: list[str] = []
    for case_id in all_cases:
        observed = {
            tuple(run.event_lists[case_id])
            for run in runs
            if case_id in run.event_lists
        }
        if len(observed) > 1:
            non_deterministic_cases.append(case_id)

    detail: dict[str, Any] = {
        "run_count": len(runs),
        "distinct_seed_count": len(distinct_seeds),
        "failed_seeds": failed_seeds,
        "non_deterministic_cases": non_deterministic_cases,
        "missing_event_logs": missing_event_logs,
    }
    if missing_event_logs:
        return CheckResult(status="MISSING", detail=detail)
    passes = (
        len(runs) >= 20
        and len(distinct_seeds) >= 20
        and not failed_seeds
        and not non_deterministic_cases
    )
    return CheckResult(status="PASS" if passes else "FAIL", detail=detail)


def _p5_cost(inputs: ParityInputs) -> CheckResult:
    missing_inputs = [
        name
        for name, value in (
            ("case_seconds_T", inputs.case_seconds_T),
            ("case_seconds_C", inputs.case_seconds_C),
        )
        if value is None
    ]
    if missing_inputs:
        return CheckResult(status="MISSING", detail={"missing": missing_inputs})

    seconds_t = cast(dict[str, float], inputs.case_seconds_T)
    seconds_c = cast(dict[str, float], inputs.case_seconds_C)
    missing_t = sorted(
        case_id for case_id in inputs.deleted_cases if case_id not in seconds_t
    )
    missing_c = sorted(
        case_id for case_id in inputs.chain_cases if case_id not in seconds_c
    )
    if missing_t or missing_c:
        return CheckResult(
            status="MISSING",
            detail={
                "missing_deleted_cases": missing_t,
                "missing_chain_cases": missing_c,
            },
        )

    total_t = sum(seconds_t[case_id] for case_id in inputs.deleted_cases)
    total_c = sum(seconds_c[case_id] for case_id in inputs.chain_cases)
    return CheckResult(
        status="PASS" if total_c <= total_t else "FAIL",
        detail={"deleted_seconds": total_t, "chain_seconds": total_c},
    )


def evaluate(inputs: ParityInputs) -> ParityReceipt:
    """Evaluate P1--P5 without performing I/O or invoking external tools."""

    checks: dict[CheckId, CheckResult] = {
        "P1": _p1_mutation(inputs),
        "P2": _p2_coverage(inputs),
        "P3": _p3_walker(inputs),
        "P4": _p4_determinism(inputs),
        "P5": _p5_cost(inputs),
    }
    verdict: Verdict = (
        "PASS" if all(result.status == "PASS" for result in checks.values()) else "FAIL"
    )
    return ParityReceipt(
        flow=inputs.flow,
        pinned_sha=inputs.pinned_sha,
        handler_modules=inputs.handler_modules,
        deleted_cases=inputs.deleted_cases,
        chain_cases=inputs.chain_cases,
        checks=checks,
        verdict=verdict,
    )


def coverage_from_json(path: str | Path) -> dict[str, FileCoverage]:
    """Load coverage.py JSON's per-file executed lines and branch arcs."""

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("files"), dict):
        raise ValueError("coverage JSON must contain an object at files")
    files = cast(dict[str, Any], raw["files"])
    result: dict[str, FileCoverage] = {}
    for filename, file_data in files.items():
        if not isinstance(file_data, dict):
            raise ValueError(f"coverage entry for {filename!r} is not an object")
        result[filename] = FileCoverage.model_validate(
            {
                "executed_lines": file_data.get("executed_lines"),
                "executed_branches": file_data.get("executed_branches"),
            }
        )
    return result


def junit_case_seconds(
    path: str | Path, *, repo_root: str | Path | None = None
) -> dict[str, float]:
    """Read JUnit times as pytest node ids.

    pytest writes ``classname`` as the dotted module path followed by any
    enclosing test classes, e.g. ``tests.unit.test_x.TestY`` for
    ``tests/unit/test_x.py::TestY::test_z``. With ``repo_root`` the longest
    dotted prefix that names an existing ``.py`` file is the module and the
    rest are classes. Without it, every dot becomes a slash (module-level
    tests only). Chain and deleted input lists must use pytest's node-id form.
    """

    root_dir = None if repo_root is None else Path(repo_root)
    root = ET.parse(path).getroot()
    result: dict[str, float] = {}
    for case in root.iter("testcase"):
        classname = case.attrib.get("classname")
        name = case.attrib.get("name")
        time = case.attrib.get("time")
        if classname is None or name is None or time is None:
            raise ValueError("every JUnit testcase needs classname, name, and time")
        parts = classname.split(".")
        module_parts, class_parts = parts, []
        if root_dir is not None:
            for split in range(len(parts), 0, -1):
                candidate = Path(*parts[:split]).with_suffix(".py")
                if (root_dir / candidate).is_file():
                    module_parts, class_parts = parts[:split], parts[split:]
                    break
        node_id = "/".join(module_parts) + ".py"
        for class_name in class_parts:
            node_id += f"::{class_name}"
        result[f"{node_id}::{name}"] = float(time)
    return result


def _write_model(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(model.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _evaluate_files(inputs_path: Path, out_path: Path) -> int:
    try:
        inputs = ParityInputs.model_validate_json(
            inputs_path.read_text(encoding="utf-8")
        )
        receipt = evaluate(inputs)
        _write_model(out_path, receipt)
    except (OSError, ValueError, TypeError, ValidationError) as exc:
        print(f"chain replacement parity: {exc}", file=sys.stderr)
        return 1
    return 0 if receipt.verdict == "PASS" else 1


def _read_lines(path: Path) -> list[str]:
    return [
        line
        for raw_line in path.read_text(encoding="utf-8").splitlines()
        if (line := raw_line.strip()) and not line.startswith("#")
    ]


def _load_delegation_config(repo_root: Path) -> dict[str, Any]:
    config_path = (
        repo_root
        / "validation"
        / "chain_replacement_receipts"
        / "delegation.inputs.yaml"
    )
    if not config_path.is_file():
        raise ValueError(
            "delegation shorthand requires "
            "validation/chain_replacement_receipts/delegation.inputs.yaml"
        )
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"delegation config is not a mapping: {config_path}")
    return cast(dict[str, Any], loaded)


def _string_list(config: dict[str, Any], *keys: str) -> list[str]:
    for key in keys:
        value = config.get(key)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return cast(list[str], value)
    raise ValueError(
        f"configuration needs a string list named one of: {', '.join(keys)}"
    )


def _run_logged(
    command: Sequence[str],
    *,
    cwd: Path,
    log_path: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log_path.write_text(completed.stdout, encoding="utf-8")
    return completed


def _measure_coverage(
    *,
    repo_root: Path,
    out_dir: Path,
    label: str,
    selection: list[str],
    deselect: list[str],
    pytest_args: list[str],
) -> dict[str, FileCoverage] | None:
    data_path = out_dir / f".coverage.{label}"
    json_path = out_dir / f"coverage-{label}.json"
    env = os.environ.copy()
    env["COVERAGE_FILE"] = str(data_path)
    run = _run_logged(
        [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--branch",
            "-m",
            "pytest",
            *pytest_args,
            *selection,
            *(f"--deselect={case_id}" for case_id in deselect),
        ],
        cwd=repo_root,
        log_path=out_dir / f"coverage-{label}.log",
        env=env,
    )
    report = _run_logged(
        [sys.executable, "-m", "coverage", "json", "-o", str(json_path)],
        cwd=repo_root,
        log_path=out_dir / f"coverage-{label}-json.log",
        env=env,
    )
    if run.returncode != 0 or report.returncode != 0:
        return None
    try:
        return coverage_from_json(json_path)
    except (OSError, ValueError, TypeError, ValidationError, json.JSONDecodeError):
        return None


def _mutmut_executable() -> str | None:
    if importlib.util.find_spec("mutmut") is None:
        return None
    sibling = Path(sys.executable).with_name("mutmut")
    if sibling.is_file():
        return str(sibling)
    return shutil.which("mutmut")


def _toml_array(values: Sequence[str]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def _copy_for_mutmut(repo_root: Path, destination: Path) -> None:
    ignored = shutil.ignore_patterns(
        ".git",
        ".venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        ".coverage*",
        ".mutmut-cache",
        "mutants",
    )
    shutil.copytree(repo_root, destination, ignore=ignored)


def _append_mutmut_config(
    scratch_repo: Path,
    *,
    handlers: list[str],
    pytest_args: list[str],
    selection: list[str],
) -> None:
    pyproject = scratch_repo / "pyproject.toml"
    original = pyproject.read_text(encoding="utf-8")
    if "[tool.mutmut]" in original:
        raise ValueError("scratch pyproject already contains [tool.mutmut]")
    # source_paths is the whole src/ tree so mutmut copies every package
    # __init__.py into mutants/; naming only the handler files there leaves
    # mutants/src/omnimarket a namespace portion that loses to the editable
    # install, the tests import the original code, and mutmut stops with "no
    # test case for any mutant" (measured on h201, 2026-09-26). only_mutate
    # restricts mutation to H. The scratch copy has no .git.
    # mutmut copies source_paths and tests into mutants/ and runs pytest there.
    # Tests that import helpers from other top-level trees (scripts/ via a
    # sys.path insert, config/, validation/) fail to collect in mutants/ unless
    # those trees are copied too, and mutmut then stops at "failed to collect
    # stats" (measured on h201, 2026-09-26). Copy every other top-level entry.
    also_copy = sorted(
        entry.name
        for entry in scratch_repo.iterdir()
        if entry.name not in {"src", "tests", "mutants", ".git", ".venv"}
    )
    config = (
        "\n[tool.mutmut]\n"
        'source_paths = ["src"]\n'
        f"also_copy = {_toml_array(also_copy)}\n"
        f"only_mutate = {_toml_array(handlers)}\n"
        "use_git_change_detection = false\n"
        f"pytest_add_cli_args = {_toml_array(pytest_args)}\n"
        "pytest_add_cli_args_test_selection = "
        f"{_toml_array(selection)}\n"
    )
    pyproject.write_text(original + config, encoding="utf-8")


def _parse_mutmut_results(output: str) -> dict[str, MutmutStatus]:
    statuses = "|".join(re.escape(status) for status in _MUTMUT_STATUSES)
    after_id = re.compile(
        rf"^\s*(?P<id>\S.*?):\s*(?P<status>{statuses})\b", re.IGNORECASE
    )
    before_id = re.compile(
        rf"^\s*(?P<status>{statuses})\s+(?P<id>\S.*?)\s*$", re.IGNORECASE
    )
    parsed: dict[str, MutmutStatus] = {}
    for line in output.splitlines():
        match = after_id.match(line) or before_id.match(line)
        if match is None:
            continue
        status = match.group("status").lower()
        parsed[match.group("id").strip()] = cast(MutmutStatus, status)
    if not parsed:
        raise ValueError("mutmut results did not contain per-mutant statuses")
    return parsed


_STATS_FAILED_MARKER = "failed to collect stats"
_FAILED_TEST_LINE = re.compile(r"^FAILED (?P<id>\S+?)(?: - .*)?$")
_MAX_STATS_RETRIES = 10


def _failed_test_ids(log_text: str) -> list[str]:
    """Return the pytest node ids a mutmut stats run reported as FAILED."""
    return sorted(
        {
            match.group("id")
            for line in log_text.splitlines()
            if (match := _FAILED_TEST_LINE.match(line.strip())) is not None
        }
    )


def _unmutated_failures(
    scratch_repo: Path,
    *,
    out_dir: Path,
    label: str,
    pytest_args: list[str],
    selection: list[str],
) -> set[str]:
    mutants_dir = scratch_repo / "mutants"
    if not mutants_dir.is_dir():
        return set()
    env = os.environ.copy()
    env["MUTANT_UNDER_TEST"] = ""
    completed = _run_logged(
        [sys.executable, "-m", "pytest", "-rf", *pytest_args, *selection],
        cwd=mutants_dir,
        log_path=out_dir / f"mutmut-unmutated-{label}.log",
        env=env,
    )
    return set(_failed_test_ids(completed.stdout))


def _measure_mutation(
    *,
    mutmut: str,
    repo_root: Path,
    scratch_parent: Path,
    out_dir: Path,
    label: str,
    handlers: list[str],
    pytest_args: list[str],
    selection: list[str],
    max_children: int,
    unmeasurable: set[str],
) -> dict[str, MutmutStatus] | None:
    """Run mutmut over H; tests that fail on UNMUTATED mutmut source are set aside.

    mutmut rewrites every function of H into a trampoline, so a test that reads
    a handler's source text (an AST scan, a line-number assertion) fails in
    mutants/ before any mutant is applied, and mutmut refuses to start. Such a
    test cannot be measured by mutation at all. It is deselected, retried at
    most ``_MAX_STATS_RETRIES`` times, and added to ``unmeasurable`` so the
    evaluator can refuse a deletion that would remove one (P1 MISSING).
    """
    for attempt in range(_MAX_STATS_RETRIES + 1):
        scratch_repo = scratch_parent / f"repo-{label}-{attempt}"
        try:
            _copy_for_mutmut(repo_root, scratch_repo)
            _append_mutmut_config(
                scratch_repo,
                handlers=handlers,
                pytest_args=pytest_args,
                selection=[
                    *selection,
                    *(f"--deselect={case_id}" for case_id in sorted(unmeasurable)),
                ],
            )
            run = _run_logged(
                [mutmut, "run", "--max-children", str(max_children)],
                cwd=scratch_repo,
                log_path=out_dir / f"mutmut-{label}.log",
            )
            if run.returncode != 0:
                failed = set(_failed_test_ids(run.stdout))
                if _STATS_FAILED_MARKER in run.stdout:
                    # mutmut's stats run stops at the first failure. Run the
                    # same selection once in mutants/ with no mutant active
                    # (MUTANT_UNDER_TEST="" calls every original function) to
                    # find every test that fails on unmutated mutmut source.
                    failed |= _unmutated_failures(
                        scratch_repo,
                        out_dir=out_dir,
                        label=f"{label}-{attempt}",
                        pytest_args=pytest_args,
                        selection=[
                            *selection,
                            *(f"--deselect={case_id}" for case_id in unmeasurable),
                        ],
                    )
                failed -= unmeasurable
                if _STATS_FAILED_MARKER in run.stdout and failed:
                    unmeasurable.update(failed)
                    shutil.rmtree(scratch_repo, ignore_errors=True)
                    continue
                return None
            results = _run_logged(
                [mutmut, "results", "--all", "true"],
                cwd=scratch_repo,
                log_path=out_dir / f"mutmut-{label}-results.log",
            )
            if results.returncode != 0:
                return None
            return _parse_mutmut_results(results.stdout)
        except (OSError, ValueError):
            return None
    return None


def _event_lists_from_jsonl(path: Path) -> dict[str, list[str]]:
    if not path.is_file():
        return {}
    result: dict[str, list[str]] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid CHAIN_EVENT_LOG JSON on line {line_number}"
            ) from exc
        if not isinstance(record, dict):
            raise ValueError(f"CHAIN_EVENT_LOG line {line_number} is not a JSON object")
        case_id = record.get("case_id")
        event_types = record.get("event_types")
        if not isinstance(case_id, str) or not isinstance(event_types, list):
            raise ValueError(
                f"CHAIN_EVENT_LOG line {line_number} needs case_id and event_types"
            )
        if not all(isinstance(item, str) for item in event_types):
            raise ValueError(
                f"CHAIN_EVENT_LOG line {line_number} has non-string event_types"
            )
        if case_id in result:
            raise ValueError(
                f"duplicate CHAIN_EVENT_LOG record for {case_id!r} on line {line_number}"
            )
        result[case_id] = cast(list[str], event_types)
    return result


def _measure_determinism(
    *,
    repo_root: Path,
    out_dir: Path,
    chain_cases: list[str],
    pytest_args: list[str],
) -> list[DeterminismRun] | None:
    runs: list[DeterminismRun] = []
    try:
        for seed in range(1, 21):
            ordered_cases = list(chain_cases)
            random.Random(seed).shuffle(ordered_cases)
            event_log = out_dir / f"chain-events-seed-{seed}.jsonl"
            event_log.unlink(missing_ok=True)
            env = os.environ.copy()
            env["PYTHONHASHSEED"] = str(seed)
            env["CHAIN_EVENT_LOG"] = str(event_log)
            completed = _run_logged(
                [sys.executable, "-m", "pytest", *pytest_args, *ordered_cases],
                cwd=repo_root,
                log_path=out_dir / f"determinism-seed-{seed}.log",
                env=env,
            )
            runs.append(
                DeterminismRun(
                    seed=seed,
                    passed=completed.returncode == 0,
                    event_lists=_event_lists_from_jsonl(event_log),
                )
            )
    except (OSError, ValueError):
        return None
    return runs


def _measure_junit(
    *,
    repo_root: Path,
    out_dir: Path,
    label: str,
    cases: list[str],
    pytest_args: list[str],
) -> dict[str, float] | None:
    junit_path = out_dir / f"junit-{label}.xml"
    try:
        completed = _run_logged(
            [
                sys.executable,
                "-m",
                "pytest",
                *pytest_args,
                *cases,
                f"--junitxml={junit_path}",
            ],
            cwd=repo_root,
            log_path=out_dir / f"junit-{label}.log",
        )
        if completed.returncode != 0:
            return None
        return junit_case_seconds(junit_path, repo_root=repo_root)
    except (OSError, ValueError, ET.ParseError):
        return None


def _git_sha(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    sha = completed.stdout.strip()
    if completed.returncode != 0 or re.fullmatch(r"[0-9a-fA-F]{40}", sha) is None:
        raise ValueError("could not resolve a 40-hex git HEAD for pinned_sha")
    return sha


def _walker_values(path: Path | None) -> list[str] | None:
    return None if path is None else _read_lines(path)


def _measure(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    try:
        if args.flow == "delegation":
            config = _load_delegation_config(repo_root)
            handlers = _string_list(config, "handlers", "handler_modules")
            deleted_cases = _string_list(config, "deleted", "deleted_cases")
            chain_cases = _string_list(config, "chain", "chain_cases")
            walker_t_value = config.get("walker_error_paths_asserted_by_T")
            walker_c_value = config.get("walker_error_paths_asserted_by_C")
            walker_t = (
                cast(list[str], walker_t_value)
                if isinstance(walker_t_value, list)
                and all(isinstance(item, str) for item in walker_t_value)
                else None
            )
            walker_c = (
                cast(list[str], walker_c_value)
                if isinstance(walker_c_value, list)
                and all(isinstance(item, str) for item in walker_c_value)
                else None
            )
        else:
            if not args.handlers or args.deleted is None or args.chain is None:
                raise ValueError(
                    "measure requires --handlers, --deleted, and --chain "
                    "unless --flow delegation is used"
                )
            handlers = cast(list[str], args.handlers)
            deleted_cases = _read_lines(args.deleted)
            chain_cases = _read_lines(args.chain)
            walker_t = _walker_values(args.walker_t)
            walker_c = _walker_values(args.walker_c)
        pytest_args = shlex.split(args.pytest_args)
        out_dir = cast(Path, args.out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        pinned_sha = _git_sha(repo_root)
    except (OSError, ValueError, TypeError) as exc:
        print(f"chain replacement parity: {exc}", file=sys.stderr)
        return 1

    mutmut = _mutmut_executable()
    if mutmut is None:
        print("mutmut not installed: uv sync --group parity", file=sys.stderr)
        return 1

    # The comparison suite is T plus C plus any --baseline path. With no
    # baseline it is exactly T plus C, which is STRICTER than the plan's
    # full-suite comparison (C alone must keep every kill and every covered
    # arc T and C had together), never looser.
    baseline = cast(list[str], args.baseline or [])
    full_selection = [*baseline, *deleted_cases, *chain_cases]
    coverage_before = _measure_coverage(
        repo_root=repo_root,
        out_dir=out_dir,
        label="before",
        selection=full_selection,
        deselect=[],
        pytest_args=pytest_args,
    )
    coverage_after = _measure_coverage(
        repo_root=repo_root,
        out_dir=out_dir,
        label="after",
        selection=full_selection,
        deselect=deleted_cases,
        pytest_args=pytest_args,
    )

    unmeasurable: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="chain-parity-mutmut-") as scratch:
        scratch_parent = Path(scratch)
        mutation_before = _measure_mutation(
            mutmut=mutmut,
            max_children=cast(int, args.max_children),
            repo_root=repo_root,
            scratch_parent=scratch_parent,
            out_dir=out_dir,
            label="before",
            handlers=handlers,
            pytest_args=pytest_args,
            selection=full_selection,
            unmeasurable=unmeasurable,
        )
        set_aside_by_before = len(unmeasurable)
        mutation_after = _measure_mutation(
            mutmut=mutmut,
            max_children=cast(int, args.max_children),
            repo_root=repo_root,
            scratch_parent=scratch_parent,
            out_dir=out_dir,
            label="after",
            handlers=handlers,
            pytest_args=pytest_args,
            selection=[
                *full_selection,
                *(f"--deselect={case_id}" for case_id in deleted_cases),
            ],
            unmeasurable=unmeasurable,
        )
        if len(unmeasurable) > set_aside_by_before and mutation_before is not None:
            # A test set aside only by the AFTER run would make the two sides
            # compare different suites; re-measure BEFORE with the final set.
            mutation_before = _measure_mutation(
                mutmut=mutmut,
                max_children=cast(int, args.max_children),
                repo_root=repo_root,
                scratch_parent=scratch_parent,
                out_dir=out_dir,
                label="before-final",
                handlers=handlers,
                pytest_args=pytest_args,
                selection=full_selection,
                unmeasurable=set(unmeasurable),
            )

    determinism_runs = _measure_determinism(
        repo_root=repo_root,
        out_dir=out_dir,
        chain_cases=chain_cases,
        pytest_args=pytest_args,
    )
    case_seconds_t = _measure_junit(
        repo_root=repo_root,
        out_dir=out_dir,
        label="deleted",
        cases=deleted_cases,
        pytest_args=pytest_args,
    )
    case_seconds_c = _measure_junit(
        repo_root=repo_root,
        out_dir=out_dir,
        label="chain",
        cases=chain_cases,
        pytest_args=pytest_args,
    )

    try:
        inputs = ParityInputs(
            flow=args.flow,
            pinned_sha=pinned_sha,
            handler_modules=tuple(handlers),
            deleted_cases=tuple(deleted_cases),
            chain_cases=tuple(chain_cases),
            mutation_before=mutation_before,
            mutation_after=mutation_after,
            mutation_killers=None,
            mutation_unmeasurable_tests=sorted(unmeasurable),
            coverage_before=coverage_before,
            coverage_after=coverage_after,
            walker_error_paths_asserted_by_T=walker_t,
            walker_error_paths_asserted_by_C=walker_c,
            determinism_runs=determinism_runs,
            case_seconds_T=case_seconds_t,
            case_seconds_C=case_seconds_c,
        )
        _write_model(out_dir / "inputs.json", inputs)
        receipt = evaluate(inputs)
        _write_model(out_dir / "receipt.json", receipt)
    except (OSError, ValueError, TypeError, ValidationError) as exc:
        print(f"chain replacement parity: {exc}", file=sys.stderr)
        return 1
    return 0 if receipt.verdict == "PASS" else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    evaluate_parser = subparsers.add_parser(
        "evaluate", help="evaluate a planted or lab-collected inputs JSON"
    )
    evaluate_parser.add_argument("--inputs", type=Path, required=True)
    evaluate_parser.add_argument("--out", type=Path, required=True)

    measure_parser = subparsers.add_parser(
        "measure", help="collect lab-only measurements and evaluate them"
    )
    measure_parser.add_argument("--flow", required=True)
    measure_parser.add_argument(
        "--handlers",
        action="append",
        help="repo-relative handler .py path; repeat for multiple handlers",
    )
    measure_parser.add_argument(
        "--deleted", type=Path, help="file of deleted pytest node ids, one per line"
    )
    measure_parser.add_argument(
        "--chain", type=Path, help="file of chain pytest node ids, one per line"
    )
    measure_parser.add_argument(
        "--pytest-args", default="", help="additional pytest arguments as one string"
    )
    measure_parser.add_argument("--out-dir", type=Path, required=True)
    measure_parser.add_argument(
        "--max-children",
        type=int,
        default=8,
        help=(
            "mutmut worker processes (default 8), bounded so a lab run does "
            "not take every core of a shared host"
        ),
    )
    measure_parser.add_argument(
        "--baseline",
        action="append",
        help=(
            "extra pytest path or node id run in BOTH the before and after "
            "suites (repeatable); default none, which compares T+C against C"
        ),
    )
    measure_parser.add_argument(
        "--walker-t",
        type=Path,
        help="optional file of walker path ids asserted by deleted tests",
    )
    measure_parser.add_argument(
        "--walker-c",
        type=Path,
        help="optional file of walker path ids asserted by chain tests",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments without running either layer."""

    return _parser().parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; all errors and every non-PASS verdict return one."""

    args = parse_args(argv)
    if args.command == "evaluate":
        return _evaluate_files(args.inputs, args.out)
    if args.command == "measure":
        return _measure(args)
    print(
        f"chain replacement parity: unknown command {args.command!r}", file=sys.stderr
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CheckResult",
    "DeterminismRun",
    "FileCoverage",
    "ParityInputs",
    "ParityReceipt",
    "coverage_from_json",
    "evaluate",
    "junit_case_seconds",
    "main",
    "parse_args",
]
