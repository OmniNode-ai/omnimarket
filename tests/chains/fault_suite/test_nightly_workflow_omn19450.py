# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19450 AC2: the fault suite runs on a schedule and publishes its result.

Reads delegation-fault-suite-nightly.yml and checks that it is scheduled, runs
this suite against ``dev``, writes a per-case job summary and keeps the JUnit
report as an artifact. The freshness of the newest scheduled run is read from
the workflow's run list, not from this test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_WORKFLOW = (
    Path(__file__).resolve().parents[3]
    / ".github"
    / "workflows"
    / "delegation-fault-suite-nightly.yml"
)


def _steps() -> list[dict[str, Any]]:
    workflow: dict[str, Any] = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    steps: list[dict[str, Any]] = workflow["jobs"]["fault-suite"]["steps"]
    return steps


def test_the_nightly_workflow_is_scheduled_without_a_pull_request_trigger() -> None:
    workflow: dict[str, Any] = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML reads the bare `on:` key as the boolean True.
    triggers: dict[str, Any] = workflow[True]
    assert [entry["cron"] for entry in triggers["schedule"]] == ["45 6 * * *"]
    assert "pull_request" not in triggers


def test_the_nightly_workflow_runs_the_fault_suite_on_dev() -> None:
    steps = _steps()
    checkout = next(
        s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"]["ref"] == "dev"
    run = next(s for s in steps if s.get("name") == "Run the five-fault suite")["run"]
    assert "uv run pytest tests/chains/fault_suite" in run
    assert "--junitxml=delegation_fault_suite_junit.xml" in run


def test_the_nightly_workflow_publishes_a_summary_and_the_junit_artifact() -> None:
    steps = _steps()
    summary = next(s for s in steps if s.get("name") == "Publish the per-case result")
    assert summary["if"] == "always()"
    assert "GITHUB_STEP_SUMMARY" in summary["run"]
    upload = next(s for s in steps if s.get("name") == "Upload JUnit report")
    assert upload["if"] == "always()"
    assert str(upload["uses"]).startswith("actions/upload-artifact@")
    assert upload["with"]["name"] == "delegation-fault-suite-junit"
    assert upload["with"]["path"] == "delegation_fault_suite_junit.xml"
