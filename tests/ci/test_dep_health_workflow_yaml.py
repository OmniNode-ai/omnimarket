# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for .github/workflows/dep-health-gate.yml GHA workflow."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "dep-health-gate.yml"


@pytest.mark.unit
class TestDepHealthWorkflowYaml:
    """Tests for the dep-health-gate GHA workflow structure."""

    def test_workflow_file_exists(self) -> None:
        """The workflow file must exist."""
        assert WORKFLOW_PATH.exists(), f"Workflow not found: {WORKFLOW_PATH}"

    def test_workflow_parses_as_valid_yaml(self) -> None:
        """The workflow must be valid YAML."""
        content = WORKFLOW_PATH.read_text()
        parsed = yaml.safe_load(content)
        assert isinstance(parsed, dict), "Workflow must parse as a YAML mapping"

    def test_workflow_triggers_on_pull_request(self) -> None:
        """Workflow must trigger on pull_request events."""
        parsed = yaml.safe_load(WORKFLOW_PATH.read_text())
        triggers = parsed.get("on", {})
        assert "pull_request" in triggers, "Workflow must trigger on pull_request"

    def test_workflow_triggers_on_merge_group(self) -> None:
        """Workflow must trigger on merge_group events (required for merge queue)."""
        parsed = yaml.safe_load(WORKFLOW_PATH.read_text())
        triggers = parsed.get("on", {})
        assert "merge_group" in triggers, (
            "Workflow must trigger on merge_group for merge queue enforcement"
        )

    def test_workflow_has_dep_health_scan_job(self) -> None:
        """Workflow must contain a job with id matching 'dep-health'."""
        parsed = yaml.safe_load(WORKFLOW_PATH.read_text())
        jobs = parsed.get("jobs", {})
        assert any("dep-health" in job_id for job_id in jobs), (
            f"No dep-health job found. Jobs: {list(jobs.keys())}"
        )

    def test_advisory_step_present(self) -> None:
        """Workflow must include an advisory (non-blocking) step."""
        content = WORKFLOW_PATH.read_text()
        # Advisory step runs without --exit-nonzero-on-findings
        assert "advisory" in content.lower() or "continue-on-error: true" in content, (
            "Workflow must have an advisory step (continue-on-error: true)"
        )

    def test_blocking_step_uses_exit_nonzero_on_findings(self) -> None:
        """The delta-blocking step must pass --exit-nonzero-on-findings."""
        content = WORKFLOW_PATH.read_text()
        assert "--exit-nonzero-on-findings" in content, (
            "Blocking step must pass --exit-nonzero-on-findings"
        )

    def test_blocking_step_does_not_have_continue_on_error_true(self) -> None:
        """The delta-blocking step must not have continue-on-error: true."""
        parsed = yaml.safe_load(WORKFLOW_PATH.read_text())
        jobs = parsed.get("jobs", {})
        for job_id, job in jobs.items():
            if "dep-health" not in job_id:
                continue
            steps = job.get("steps", [])
            for step in steps:
                run = str(step.get("run", ""))
                # Check any step that is the blocking delta step
                if "--exit-nonzero-on-findings" in run:
                    continue_on_error = step.get("continue-on-error", False)
                    assert not continue_on_error, (
                        f"Blocking step '{step.get('name')}' must not have "
                        "continue-on-error: true"
                    )

    def test_uses_uv_sync_not_pip_install(self) -> None:
        """Workflow must use uv sync --locked, not pip install graphify."""
        content = WORKFLOW_PATH.read_text()
        assert "pip install graphify" not in content, (
            "Workflow must not use pip install graphify — use uv sync --locked"
        )
        assert "uv sync" in content, (
            "Workflow must use uv sync for deterministic dependency installation"
        )

    def test_uses_baseline_conditional(self) -> None:
        """Workflow must use hashFiles conditional for baseline-first rollout."""
        content = WORKFLOW_PATH.read_text()
        assert "hashFiles" in content, (
            "Workflow must use hashFiles('.onex_state/dep_health_baseline.json') "
            "conditional for phased rollout"
        )
        assert "dep_health_baseline.json" in content, (
            "Workflow must reference dep_health_baseline.json for baseline check"
        )

    def test_no_pip_install_in_workflow(self) -> None:
        """No pip install commands anywhere in the workflow."""
        content = WORKFLOW_PATH.read_text()
        assert "pip install" not in content, (
            "Workflow must not use pip install — use uv sync"
        )
