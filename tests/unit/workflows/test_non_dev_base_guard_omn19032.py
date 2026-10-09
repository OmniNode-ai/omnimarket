# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise the deployed workflow steps for feature-base arming (OMN-19032)."""

from __future__ import annotations

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
