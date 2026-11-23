# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Negative controls for OMN-26228, re-runnable on origin/dev. Each test runs the guarded test in a child pytest with one defect planted by the omn26228_mutants plugin, and asserts the guarded test passes or fails exactly as the acceptance criteria say. A plugin that could not plant its defect writes no report, which fails the test."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.slow]

REPO_ROOT = Path(__file__).resolve().parents[4]
TERMINAL_FILE = "tests/unit/nodes/node_delegate_skill_orchestrator/test_pre_dispatch_failure_terminal_omn17427.py"
RUNTIME_CASE = (
    TERMINAL_FILE
    + "::test_real_handler_terminal_reaches_the_waiter_on_its_declared_topic[runtime]"
)
MARKET_CASE = (
    TERMINAL_FILE
    + "::test_real_handler_terminal_reaches_the_waiter_on_its_declared_topic[market]"
)
RUNTIME_SELECTOR = (
    "test_real_handler_terminal_reaches_the_waiter_on_its_declared_topic and runtime"
)
ROUTING_FILE = "tests/unit/routing/test_handler_routing_intent_dod_overlay.py"
PLUGIN = "tests.unit.nodes.node_delegate_skill_orchestrator.omn26228_mutants"
CHILDREN = 4
RUNS_PER_CHILD = 5

_BURN = [sys.executable, "-c", "while True: pass"]


def _child_command(node_ids: list[str], junit: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        *node_ids,
        "--keep-duplicates",
        "-o",
        "xfail_strict=true",
        "-q",
        "-n",
        "0",
        "--no-cov",
        "-p",
        "no:randomly",
        "-p",
        "no:cacheprovider",
        "-p",
        PLUGIN,
        f"--junitxml={junit}",
    ]


def _child_env(mode: str, report: Path, *, repeat: int = 1) -> dict[str, str]:
    env = dict(os.environ)
    env["OMN26228_MUTANT"] = mode
    env["OMN26228_REPORT"] = str(report)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_XDIST_WORKER", None)
    if repeat > 1:
        env["OMN26228_REPEAT"] = str(repeat)
    else:
        env.pop("OMN26228_REPEAT", None)
    if mode == "slow_scan":
        env["OMN26228_SLOW_SCAN_SECONDS"] = "11"
    else:
        env.pop("OMN26228_SLOW_SCAN_SECONDS", None)
    return env


def _cases(junit: Path) -> dict[str, tuple[str, str]]:
    if not junit.exists():
        pytest.fail(
            f"the child pytest never wrote its JUnit XML at {junit}; "
            "the defect was not planted or the child did not run"
        )
    root = ET.parse(junit).getroot()
    cases: dict[str, tuple[str, str]] = {}
    for tc in root.iter("testcase"):
        name = tc.get("name", "")
        outcome = "passed"
        text = ""
        for child in tc:
            if child.tag in ("failure", "error", "skipped"):
                outcome = child.tag
                text = child.get("message", "") or ""
                if child.text:
                    text += "\n" + child.text
                break
        cases[name] = (outcome, text)
    return cases


def _per_test(report: dict[str, object]) -> dict[str, int]:
    per_test = report.get("per_test")
    if not isinstance(per_test, dict):
        pytest.fail(
            "the plugin report is missing a usable 'per_test' mapping; "
            "the defect was not planted"
        )
    return per_test


def _report(path: Path) -> dict[str, object]:
    if not path.exists():
        pytest.fail(
            f"the plugin never wrote its report at {path}; the defect was not planted"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def test_ac1_runtime_case_passes_20_runs_on_a_loaded_host_with_the_scan_past_the_deadline(
    tmp_path: Path,
) -> None:
    """Proves AC1 (the delayed scan still lets every run pass on a loaded host) by
    planting the slow_scan defect: the scan sleeps 11 s, longer than the test's
    10 s dispatch deadline, standing in for the slowest loaded runner; if the
    scan were inside the deadline every run would time out; CPU burner processes, one per four of the host's cores (at least one), at normal priority,
    and four concurrent children keep the host loaded; each child
    discovers routes and runs the real scan once and repeats only the delayed
    scan and the timed dispatch, so the 20 runs fit inside the hosted per-check
    timeout; the directory-wide load leg named in the criterion is not
    used because the closer does not run directory-wide suites (Operating Rule
    21). Each child repeats the runtime case RUNS_PER_CHILD times in one process
    through the plugin's OMN26228_REPEAT, because pytest runs a duplicated node
    id only once."""
    n_burners = max(1, (os.cpu_count() or 4) // 4)
    burners: list[subprocess.Popen] = []
    children: list[tuple[subprocess.Popen, Path, Path, Path, object]] = []
    try:
        for _ in range(n_burners):
            burners.append(
                subprocess.Popen(
                    _BURN, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            )
        for i in range(CHILDREN):
            report = tmp_path / f"report_{i}.json"
            junit = tmp_path / f"junit_{i}.xml"
            log = tmp_path / f"out_{i}.log"
            env = _child_env("slow_scan", report, repeat=RUNS_PER_CHILD)
            cmd = _child_command([TERMINAL_FILE, "-k", RUNTIME_SELECTOR], junit)
            log_file = log.open("w", encoding="utf-8")
            proc = subprocess.Popen(
                cmd,
                cwd=REPO_ROOT,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            children.append((proc, report, junit, log, log_file))

        deadline = time.monotonic() + 240
        for proc, report, junit, log, log_file in children:
            proc.wait(timeout=max(1.0, deadline - time.monotonic()))
            log_file.close()
            out = log.read_text(encoding="utf-8", errors="replace")
            assert all(b.poll() is None for b in burners), (
                "a CPU burner died before the run finished; the host was not "
                f"kept loaded for the whole run. Child output tail:\n{out[-3000:]}"
            )
            assert proc.returncode == 0, (
                f"child exited with {proc.returncode}, expected 0. "
                f"Output tail:\n{out[-3000:]}"
            )
            cases = _cases(junit)
            assert len(cases) == RUNS_PER_CHILD, (
                f"expected {RUNS_PER_CHILD} cases in the JUnit XML, got {len(cases)}. "
                f"Output tail:\n{out[-3000:]}"
            )
            for name, (outcome, text) in cases.items():
                assert outcome == "passed", (
                    f"case {name} outcome {outcome!r} (expected 'passed'); "
                    f"case text: {text!r}. Output tail:\n{out[-3000:]}"
                )
            per_test = _per_test(_report(report))
            for name in cases:
                assert any(key.split("::")[-1] == name for key in per_test), (
                    f"case {name} has no per_test hit; not every run went "
                    f"through the delayed scan. Output tail:\n{out[-3000:]}"
                )
    finally:
        for proc, _, _, _, _ in children:
            if proc.poll() is None:
                proc.kill()
        for b in burners:
            if b.poll() is None:
                b.kill()
        cleanup_deadline = time.monotonic() + 10
        for proc, _, _, _, _ in children:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=max(0.1, cleanup_deadline - time.monotonic()))
        for b in burners:
            with contextlib.suppress(subprocess.TimeoutExpired):
                b.wait(timeout=max(0.1, cleanup_deadline - time.monotonic()))
        for _, _, _, _, log_file in children:
            if log_file is not None and not log_file.closed:
                log_file.close()


def test_ac1_ac3_terminal_test_fails_when_the_terminal_is_never_published(
    tmp_path: Path,
) -> None:
    """Proves AC1's second half and AC3's terminal control by planting the no_terminal defect: the in-memory bus drops every terminal topic, so each waiter reports status 'timeout' instead of the failed terminal, and both the runtime and market cases fail at their status assertion."""
    report = tmp_path / "report.json"
    junit = tmp_path / "junit.xml"
    env = _child_env("no_terminal", report)
    cmd = _child_command([RUNTIME_CASE, MARKET_CASE], junit)
    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        timeout=100,
        capture_output=True,
        text=True,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, (
        f"expected returncode 1, got {proc.returncode}. Output tail:\n{out[-3000:]}"
    )
    cases = _cases(junit)
    expected = {
        "test_real_handler_terminal_reaches_the_waiter_on_its_declared_topic[runtime]",
        "test_real_handler_terminal_reaches_the_waiter_on_its_declared_topic[market]",
    }
    assert set(cases) == expected, (
        f"expected cases {expected}, got {set(cases)}. Output tail:\n{out[-3000:]}"
    )
    for name in expected:
        outcome, text = cases[name]
        assert outcome == "failure", (
            f"case {name} outcome {outcome!r} (expected 'failure'); "
            f"case text: {text!r}. Output tail:\n{out[-3000:]}"
        )
        assert "assert 'timeout' == 'failed'" in text, (
            f"case {name} did not fail at its status assertion; "
            f"case text: {text!r}. Output tail:\n{out[-3000:]}"
        )
    per_test = _per_test(_report(report))
    for name in expected:
        assert any(key.split("::")[-1] == name for key in per_test), (
            f"case {name} has no per_test hit; the defect was not planted. "
            f"Output tail:\n{out[-3000:]}"
        )


def test_ac3_routing_test_fails_when_the_static_decision_is_wrong(
    tmp_path: Path,
) -> None:
    """Proves AC3 (the routing test fails when the static decision is wrong)
    by planting the routing_reversed defect: the static decision is reversed,
    so the routing test fails with an assertion that the decision is 'local'."""
    report = tmp_path / "report.json"
    junit = tmp_path / "junit.xml"
    env = _child_env("routing_reversed", report)
    cmd = _child_command([ROUTING_FILE], junit)
    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        timeout=60,
        capture_output=True,
        text=True,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, (
        f"expected returncode 1, got {proc.returncode}. Output tail:\n{out[-3000:]}"
    )
    cases = _cases(junit)
    for name, (outcome, text) in cases.items():
        assert outcome not in ("skipped", "error"), (
            f"case {name} outcome {outcome!r} (expected 'failure' or 'passed'); "
            f"case text: {text!r}. Output tail:\n{out[-3000:]}"
        )
    name = "test_no_reader_wired_decides_exactly_as_before"
    assert name in cases, (
        f"case {name} is not in the JUnit XML. Output tail:\n{out[-3000:]}"
    )
    outcome, text = cases[name]
    assert outcome == "failure", (
        f"case {name} outcome {outcome!r} (expected 'failure'); "
        f"case text: {text!r}. Output tail:\n{out[-3000:]}"
    )
    assert "== 'local'" in text, (
        f"case {name} did not fail with the == 'local' assertion; "
        f"case text: {text!r}. Output tail:\n{out[-3000:]}"
    )
    per_test = _per_test(_report(report))
    assert any(key.split("::")[-1] == name for key in per_test), (
        f"case {name} has no per_test hit; the defect was not planted. "
        f"Output tail:\n{out[-3000:]}"
    )
