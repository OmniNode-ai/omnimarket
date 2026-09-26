# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract walker is wired REPORT-ONLY, and says so (OMN-19554, epic OMN-19546).

Golden-chain validation layer plan r4 Phase 3 is report-only by operator ruling
(2026-09-25). Rule 5 still applies: the walker is wired as a CI job and a
pre-commit hook in the same change, and both surfaces name themselves
report-only so nobody mistakes them for a gate. Asserted here rather than
trusted from a PR body:

1. the workflow job's display name ends in ``(report-only)``, the job is
   ``continue-on-error``, and that setting carries the advisory-job annotation;
2. the job is not a CI Summary expected context, so it cannot block a merge;
3. the pre-commit hook consumes omnibase_core's ``report-contract-walk`` at the
   same omnibase_core commit the workflow installs (the two pins move together).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "contract-walker.yml"
PRECOMMIT = REPO_ROOT / ".pre-commit-config.yaml"
CI_SUMMARY = REPO_ROOT / "scripts" / "ci" / "ci_summary_gate.py"
ADVISORY_ANNOTATION_RE = re.compile(
    r"#\s*advisory-ok:\s*(?P<ticket>OMN-\d+)\s+(?P<reason>\S.*?)\s*$"
)


def _workflow() -> dict[str, object]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _job() -> dict[str, object]:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    job = jobs["contract-walker"]
    assert isinstance(job, dict)
    return job


def test_job_is_named_report_only_and_cannot_fail_the_build() -> None:
    job = _job()
    assert job["name"] == "contract-walker (report-only)"
    assert job["continue-on-error"] is True
    steps = job["steps"]
    assert isinstance(steps, list)
    runs = "\n".join(str(step.get("run", "")) for step in steps)
    assert "omnibase_core.validation.validator_contract_walker" in runs
    assert "--json-out contract-walk-report.json" in runs


def test_continue_on_error_has_advisory_job_annotation() -> None:
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    job_start = lines.index("  contract-walker:")
    job_end = next(
        (
            index
            for index in range(job_start + 1, len(lines))
            if re.fullmatch(r"  \S[^:]*:", lines[index])
        ),
        len(lines),
    )
    continue_on_error_lines = [
        index
        for index in range(job_start + 1, job_end)
        if re.fullmatch(r"\s*continue-on-error:\s*true(?:\s+#.*)?", lines[index])
    ]
    assert len(continue_on_error_lines) == 1

    (setting_index,) = continue_on_error_lines
    candidates = [lines[setting_index]]
    if re.fullmatch(r"\s*#.*", lines[setting_index - 1]):
        candidates.append(lines[setting_index - 1])

    annotations = [
        match
        for line in candidates
        if (match := ADVISORY_ANNOTATION_RE.search(line)) is not None
    ]
    assert len(annotations) == 1
    assert annotations[0].group("ticket") == "OMN-19554"


def test_job_is_not_a_ci_summary_expected_context() -> None:
    text = CI_SUMMARY.read_text(encoding="utf-8")
    assert "contract-walker" not in text


def test_precommit_hook_pins_the_same_core_commit_as_the_workflow() -> None:
    config = yaml.safe_load(PRECOMMIT.read_text(encoding="utf-8"))
    walker_repos = [
        repo
        for repo in config["repos"]
        if any(
            hook.get("id") == "report-contract-walk" for hook in repo.get("hooks", [])
        )
    ]
    assert len(walker_repos) == 1
    (repo,) = walker_repos
    assert repo["repo"] == "https://github.com/OmniNode-ai/omnibase_core"
    env = _workflow()["env"]
    assert isinstance(env, dict)
    assert repo["rev"] == env["OMNIBASE_CORE_WALKER_REV"]
