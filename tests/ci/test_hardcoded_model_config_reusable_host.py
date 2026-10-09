# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protect the reusable hardcoded-model-config host contract (OMN-16647)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github/workflows/hardcoded-model-config-reusable.yml"
)
JOB_ID = "hardcoded-model-config"


@pytest.fixture
def workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.mark.unit
def test_workflow_call_requires_only_core_ref(workflow: dict[str, Any]) -> None:
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_call"}
    inputs = triggers["workflow_call"]["inputs"]
    assert set(inputs) == {"core_ref"}
    assert inputs["core_ref"]["type"] == "string"
    assert inputs["core_ref"]["required"] is True
    assert "default" not in inputs["core_ref"]


@pytest.mark.unit
def test_caller_check_context_is_unchanged(workflow: dict[str, Any]) -> None:
    assert set(workflow["jobs"]) == {JOB_ID}
    assert workflow["jobs"][JOB_ID]["name"] == JOB_ID


@pytest.mark.unit
def test_jobs_and_steps_cannot_skip_or_bypass(workflow: dict[str, Any]) -> None:
    for job in workflow["jobs"].values():
        for entry in [job, *job["steps"]]:
            assert "continue-on-error" not in entry
            assert "if" not in entry


@pytest.mark.unit
def test_every_action_is_pinned_to_a_full_sha() -> None:
    refs = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", WORKFLOW.read_text(), re.M)
    assert refs
    assert all(re.fullmatch(r"[^@]+@[0-9a-fA-F]{40}", ref) for ref in refs)


@pytest.mark.unit
def test_guard_invocation_and_pull_request_ratchet(workflow: dict[str, Any]) -> None:
    guard = next(
        step
        for step in workflow["jobs"][JOB_ID]["steps"]
        if step["name"] == "Run the hardcoded-model-config guard"
    )
    run = guard["run"]
    assert (
        'uvx --from "git+https://github.com/OmniNode-ai/omnibase_core@${CORE_REF}"'
        in run
    )
    assert (
        "python -m omnibase_core.validation.hardcoded_model_config."
        "runtime_hardcoded_model_config" in run
    )
    assert "args=(--all --baseline config/hardcoded_model_config_baseline.yaml)" in run
    assert '"${args[@]}"' in run
    assert re.search(
        r'if \[ "\$\{EVENT_NAME\}" = "pull_request" \] && '
        r'\[ -n "\$\{PR_BASE_SHA\}" \]; then\s*'
        r'args\+=\(--base "\$\{PR_BASE_SHA\}"\)\s*fi',
        run,
    )
    commands = "\n".join(line for line in run.splitlines() if not line.startswith("#"))
    assert len(re.findall(r"--base\b", commands)) == 1
    assert guard["env"] == {
        "CORE_REF": "${{ inputs.core_ref }}",
        "EVENT_NAME": "${{ github.event_name }}",
        "PR_BASE_SHA": "${{ github.event.pull_request.base.sha }}",
    }


@pytest.mark.unit
def test_checkout_has_history_and_no_persisted_credentials(
    workflow: dict[str, Any],
) -> None:
    checkout = next(
        step
        for step in workflow["jobs"][JOB_ID]["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["persist-credentials"] is False


@pytest.mark.unit
def test_core_ref_validation_runs_before_checkout(workflow: dict[str, Any]) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to execute the workflow validation step")
    steps = workflow["jobs"][JOB_ID]["steps"]
    index = next(i for i, step in enumerate(steps) if "40-hex" in step["name"])
    checkout_index = next(
        i
        for i, step in enumerate(steps)
        if step.get("uses", "").startswith("actions/checkout@")
    )
    assert index < checkout_index
    assert steps[index]["env"]["CORE_REF"] == "${{ inputs.core_ref }}"
    for core_ref, expected in [("abc", 1), ("a" * 40, 0)]:
        result = subprocess.run(
            [bash, "-c", steps[index]["run"]],
            env={**os.environ, "CORE_REF": core_ref},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == expected, result.stdout + result.stderr
