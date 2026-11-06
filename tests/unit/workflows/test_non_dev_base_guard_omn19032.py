# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise the deployed workflow steps for feature-base arming (OMN-19032)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.unit


def _workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())


def _run_step(
    tmp_path: Path, script: str, response: str, **env: str
) -> subprocess.CompletedProcess[str]:
    # The executable is only the GitHub transport; run the actual workflow policy.
    gh = tmp_path / "gh"
    gh.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$@" > "$TEST_CALL_LOG"\n'
        'printf "%s\\n" "$TEST_RESPONSE"\nexit "${TEST_API_EXIT:-0}"\n'
    )
    gh.chmod(0o755)
    return subprocess.run(
        ["bash", "-e", "-c", script],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "TEST_RESPONSE": response,
            "TEST_CALL_LOG": str(tmp_path / "gh-call.txt"),
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("base", ["feature/parent", "fix/parent"])
def test_automated_arming_refuses_feature_base(tmp_path: Path, base: str) -> None:
    steps = _workflow("auto-merge.yml")["jobs"]["auto-merge"]["steps"]
    guard = next(
        step for step in steps if step.get("name") == "Refuse feature-base auto-merge"
    )
    assert steps.index(guard) < next(
        i for i, step in enumerate(steps) if step.get("name") == "Enable auto-merge"
    )
    result = _run_step(
        tmp_path, guard["run"], base, PR="123", REPO="OmniNode-ai/omnimarket"
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "::error::" in result.stdout
    call = (tmp_path / "gh-call.txt").read_text()
    assert "baseRefName" in call


@pytest.mark.parametrize("base", ["dev", "main"])
def test_automated_arming_preserves_integration_bases(
    tmp_path: Path, base: str
) -> None:
    steps = _workflow("auto-merge.yml")["jobs"]["auto-merge"]["steps"]
    guard = next(
        step for step in steps if step.get("name") == "Refuse feature-base auto-merge"
    )
    result = _run_step(
        tmp_path, guard["run"], base, PR="123", REPO="OmniNode-ai/omnimarket"
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("response", "api_exit"), [("", "0"), ("null", "0"), ("", "1")]
)
def test_automated_arming_fails_closed_on_unreadable_base(
    tmp_path: Path, response: str, api_exit: str
) -> None:
    steps = _workflow("auto-merge.yml")["jobs"]["auto-merge"]["steps"]
    guard = next(
        step for step in steps if step.get("name") == "Refuse feature-base auto-merge"
    )
    result = _run_step(
        tmp_path,
        guard["run"],
        response,
        TEST_API_EXIT=api_exit,
        PR="123",
        REPO="OmniNode-ai/omnimarket",
    )
    assert result.returncode != 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("armed", "expected"), [("true", 1), ("false", 0), ("null", 1), ("", 1)]
)
def test_declared_stack_reads_live_auto_merge_state(
    tmp_path: Path, armed: str, expected: int
) -> None:
    guard = _workflow("non-dev-base-guard.yml")["jobs"]["non-dev-base-guard"]["steps"][
        0
    ]
    result = _run_step(
        tmp_path,
        guard["run"],
        armed,
        BASE_REF="feature/parent",
        HEAD_REF="feature/child",
        PR_BODY="Stacked-Parent: #123",
        PR="124",
        REPO="OmniNode-ai/omnimarket",
    )
    assert result.returncode == expected, result.stdout + result.stderr
    call = (tmp_path / "gh-call.txt").read_text()
    assert "repos/OmniNode-ai/omnimarket/pulls/124" in call
    assert 'has("auto_merge")' in call


@pytest.mark.parametrize(
    ("base", "body", "expected"),
    [("dev", "", 0), ("main", "", 0), ("feature/parent", "", 1)],
)
def test_base_guard_preserves_existing_verdicts(
    tmp_path: Path, base: str, body: str, expected: int
) -> None:
    guard = _workflow("non-dev-base-guard.yml")["jobs"]["non-dev-base-guard"]["steps"][
        0
    ]
    result = _run_step(
        tmp_path,
        guard["run"],
        "",
        BASE_REF=base,
        HEAD_REF="feature/child",
        PR_BODY=body,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    assert not (tmp_path / "gh-call.txt").exists()


def test_declared_stack_refuses_failed_live_read(tmp_path: Path) -> None:
    guard = _workflow("non-dev-base-guard.yml")["jobs"]["non-dev-base-guard"]["steps"][
        0
    ]
    result = _run_step(
        tmp_path,
        guard["run"],
        "false",
        TEST_API_EXIT="1",
        BASE_REF="feature/parent",
        HEAD_REF="feature/child",
        PR_BODY="Stacked-Parent: #123",
        PR="124",
        REPO="OmniNode-ai/omnimarket",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Could not read current auto-merge state" in result.stdout


def test_guard_rechecks_auto_merge_state_changes() -> None:
    workflow = _workflow("non-dev-base-guard.yml")
    types = workflow.get("on", workflow.get(True))["pull_request"]["types"]
    assert {
        "edited",
        "synchronize",
        "auto_merge_enabled",
        "auto_merge_disabled",
        "reopened",
    } <= set(types)
    assert workflow["permissions"]["pull-requests"] == "read"


def test_refusal_cannot_be_skipped_by_an_arming_trigger() -> None:
    steps = _workflow("auto-merge.yml")["jobs"]["auto-merge"]["steps"]
    guard = next(
        step for step in steps if step.get("name") == "Refuse feature-base auto-merge"
    )
    arm = next(step for step in steps if step.get("name") == "Enable auto-merge")
    assert guard["if"] == arm["if"]
    assert "steps.hold_gate.outputs.hold == 'false'" in guard["if"]
    assert "outputs.actor" not in guard["if"]
    assert not guard.get("continue-on-error", False)


def test_regression_is_wired_in_required_ci_and_precommit() -> None:
    command = "uv run --frozen pytest tests/unit/workflows/test_non_dev_base_guard_omn19032.py -q -m unit"
    ci = _workflow("ci.yml")
    assert any(step.get("run") == command for step in ci["jobs"]["lint"]["steps"])
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hook = next(
        hook
        for repo in config["repos"]
        for hook in repo["hooks"]
        if hook["id"] == "non-dev-base-guard-regression"
    )
    assert hook["entry"] == command
    assert hook["pass_filenames"] is False


_RECORDED_CHECK_SUITE_RUNS = json.loads(
    (ROOT / "tests/fixtures/auto_merge_check_suite_runs.json").read_text()
)["runs"]


@pytest.mark.parametrize(
    "recorded", _RECORDED_CHECK_SUITE_RUNS, ids=lambda run: str(run["id"])
)
def test_recorded_pr_automation_does_not_run_on_default_branch_head(
    recorded: dict,
) -> None:
    # These are workflow-run records, not the associated PR's head. For example,
    # run 41925092641 checked out dev 18ec771 while trying to arm draft PR #5094.
    assert recorded["head_branch"] == "dev"
    assert recorded["event"] == "check_suite"
    assert recorded["conclusion"] == "failure"
    workflow = _workflow("auto-merge.yml")
    triggers = workflow.get("on", workflow.get(True))
    assert recorded["event"] not in triggers, (
        f"run {recorded['id']} attaches PR automation to unrelated dev commit "
        f"{recorded['head_sha']}"
    )


@pytest.mark.parametrize(
    ("event", "action"),
    [
        ("pull_request", "opened"),
        ("pull_request", "ready_for_review"),
        ("pull_request_review", "submitted"),
        ("pull_request_review", "dismissed"),
        ("workflow_dispatch", None),
    ],
)
def test_pr_scoped_arming_triggers_remain(event: str, action: str | None) -> None:
    workflow = _workflow("auto-merge.yml")
    triggers = workflow.get("on", workflow.get(True))
    assert event in triggers
    if action is not None:
        assert action in triggers[event]["types"]


@pytest.mark.parametrize(
    "recorded",
    json.loads((ROOT / "tests/fixtures/auto_merge_check_suite_runs.json").read_text())[
        "recorded_red_checks"
    ],
    ids=lambda check: check["name"],
)
def test_tag_oracle_still_rejects_recorded_red_checks(recorded: dict) -> None:
    from omnimarket.nodes.node_dev_tag_pin_mover_compute.handlers.handler_dev_tag_green_verdict import (
        required_checks_verdict,
    )

    # OMN-22497: the oracle judges the base branch's required checks, so a real
    # red is a red required check; the recorded red still reads red when required.
    required = frozenset({recorded["name"]})
    red = [(recorded["name"], recorded["status"], recorded["conclusion"])]
    assert required_checks_verdict(red, required) is False
    green = [(recorded["name"], "completed", "success")]
    assert required_checks_verdict(green, required) is True


def test_recorded_failure_passed_the_feature_base_guard(tmp_path: Path) -> None:
    recorded = json.loads(
        (ROOT / "tests/fixtures/auto_merge_check_suite_runs.json").read_text()
    )["recorded_failure"]
    steps = _workflow("auto-merge.yml")["jobs"]["auto-merge"]["steps"]
    guard = next(
        step for step in steps if step.get("name") == "Refuse feature-base auto-merge"
    )
    result = _run_step(
        tmp_path,
        guard["run"],
        recorded["baseRefName"],
        PR=str(recorded["pr_number"]),
        REPO="OmniNode-ai/omnimarket",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == recorded["guard_stdout"]
    assert "Pull request is a draft" in recorded["enable_stdout"]
