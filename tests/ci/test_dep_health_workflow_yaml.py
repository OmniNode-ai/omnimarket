# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for .github/workflows/dep-health-gate.yml GHA workflow."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

#: The annotation form omniclaude's advisory_job_gate.py (OMN-18777) requires of
#: any `continue-on-error: true`. Vendored as a regex rather than imported: that
#: gate is fetched from omniclaude at CI time and is not importable from here.
_ADVISORY_OK = re.compile(r"#\s*advisory-ok:\s*OMN-\d+\s+\S")

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

    def test_blocking_step_uses_delta_mode(self) -> None:
        """The delta-blocking step must pass --delta-mode and baseline path."""
        content = WORKFLOW_PATH.read_text()
        assert "--delta-mode" in content, "Blocking step must pass --delta-mode"
        assert "--baseline-path" in content, "Blocking step must pass --baseline-path"

    def test_blocking_step_is_advisory_only_when_annotated(self) -> None:
        """The delta step blocks, unless it carries an `advisory-ok` annotation.

        WHAT THIS PROTECTED, UNCHANGED: a `continue-on-error: true` must not
        appear on the delta step silently. That is still refused below.

        WHAT CHANGED (OMN-19889). The assertion was unconditional, so there was
        no way to unblock the repository without deleting the guard. That
        mattered on 2026-09-27: OMN-19677 (ff67f869) shrank
        .onex_state/dep_health_baseline.json from 584 entries to 49, switched
        the gate to block on any NEW finding rather than the net delta, and
        removed the advisory fallback. The 535 de-banked findings then read as
        new against a blocking gate with no non-blocking path, `dep-health /
        scan` failed on eight consecutive runs across seven authors' branches
        including branches that touch no topic, and nothing in omnimarket could
        merge.

        The escape is the one the org already enforces everywhere else rather
        than a new one invented here: omniclaude's `advisory_job_gate.py`
        (OMN-18777) refuses any `continue-on-error: true` that is neither
        baselined nor annotated `# advisory-ok: OMN-nnnnn <reason>`, in exactly
        the two positions accepted below. So an annotated escape is already
        auditable org-wide and already has to name a ticket; this test stops
        being the one place that forbids what that gate is built to govern.

        A bare `continue-on-error: true` is still a failure, which is the half
        worth keeping. Authority: the shrink-only direction rests on the
        operator ruling cited by OMN-19677
        (docs/tracking/ROLLING_WORK_LEDGER.md:5194); this test is not that
        ruling and never was, and re-banking the 535 remains an open question
        on OMN-19889 rather than something this change settles.
        """
        lines = WORKFLOW_PATH.read_text().splitlines()
        parsed = yaml.safe_load("\n".join(lines))
        jobs = parsed.get("jobs", {})
        checked = 0
        for job_id, job in jobs.items():
            if "dep-health" not in job_id:
                continue
            for step in job.get("steps", []):
                run = str(step.get("run", ""))
                if "--delta-mode" not in run:
                    continue
                checked += 1
                if not step.get("continue-on-error", False):
                    continue
                # Same two positions omniclaude's advisory_job_gate.py accepts:
                # the setting's own line, or the comment line directly above.
                # Anything further away drifts onto the wrong key on the next
                # edit, which is why neither reader looks further.
                name = step.get("name")
                annotated = any(
                    _ADVISORY_OK.search(line)
                    for index, line in enumerate(lines)
                    if "continue-on-error" in line
                    and (
                        _ADVISORY_OK.search(line)
                        or (
                            index > 0
                            and lines[index - 1].lstrip().startswith("#")
                            and _ADVISORY_OK.search(lines[index - 1])
                        )
                    )
                )
                assert annotated, (
                    f"Step '{name}' has continue-on-error: true with no "
                    "`# advisory-ok: OMN-nnnnn <reason>` annotation. A gate "
                    "that runs and cannot fail must name the ticket that "
                    "removes it, or it becomes permanent by omission "
                    "(OMN-18777, OMN-19889)."
                )
        assert checked, (
            "no dep-health step runs the sweep with --delta-mode; the gate's "
            "delta phase has been renamed or removed"
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
