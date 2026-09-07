# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every OMN-18013 gate is ENFORCED on this repo, and this test says exactly how.

Operating Rule 5, and the operator ruling of 2026-09-06 (firm): a detection tool that is
not a pre-merge gate is advisory and gets ignored. So each gate in the closure set must
carry all three surfaces at once, and this asserts each one rather than trusting the PR
body:

1. a **pre-commit hook** with the gate's id, so it fires before the commit exists;
2. a **CI job** of the same name in ``.github/workflows/contract-topic-closure.yml``;
3. an entry in ``scripts/ci/ci_summary_gate.py::EXPECTED_EXTERNAL_CONTEXTS``.

Surface 3 is the one that actually blocks a merge here, and it is worth being precise
about why. ``omnimarket``'s ``dev`` branch protection lists 32 required contexts, and the
closure gates are deliberately not among them: the required context they ride is the
``CI Summary`` umbrella, whose layer-4 check resolves every name in
``EXPECTED_EXTERNAL_CONTEXTS`` against the head SHA's check-runs and fails the umbrella if
one is missing, still running, or not a passing conclusion. That is the same mechanism
``subscriber-dispatcher-resolution`` and ``dispatcher-route-coverage`` already use in this
repo, and it is why the gates need no branch-protection change to be required.

The failure mode this test exists to prevent is the quiet one: a job renamed in the
workflow, or a hook id changed, leaves the L4 name pointing at nothing. L4 fails closed on
a missing context, so that mistake would block every PR rather than silently pass — but it
would block them for a reason nobody could read. Naming the coupling here makes the rename
a one-line test failure instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github/workflows/contract-topic-closure.yml"
PRECOMMIT = REPO_ROOT / ".pre-commit-config.yaml"
CI_SUMMARY_GATE = REPO_ROOT / "scripts/ci/ci_summary_gate.py"

# The gate id IS the pre-commit hook id, the workflow job id, the job's display name and
# the CI Summary context — deliberately one string, so the three surfaces cannot drift
# apart by a rename.
CLOSURE_GATES: tuple[str, ...] = (
    "mixed-category-routing",
    "handler-event-type-source",
    "no-literal-event-type-in-tests",
    "no-baseline-refreeze",
)


def _workflow() -> dict[str, object]:
    return yaml.safe_load(WORKFLOW.read_text())


def _precommit_hook_ids() -> set[str]:
    config = yaml.safe_load(PRECOMMIT.read_text())
    return {
        hook["id"]
        for repo in config.get("repos", [])
        for hook in repo.get("hooks", [])
        if "id" in hook
    }


def _expected_external_contexts() -> tuple[str, ...]:
    """Read the tuple literal without importing the script (it has CI-only imports)."""
    source = CI_SUMMARY_GATE.read_text()
    match = re.search(
        r"EXPECTED_EXTERNAL_CONTEXTS:\s*tuple\[str, \.\.\.\]\s*=\s*\((?P<body>.*?)\n\)",
        source,
        re.DOTALL,
    )
    assert match is not None, "EXPECTED_EXTERNAL_CONTEXTS tuple not found"
    return tuple(re.findall(r'"([^"]+)"', match.group("body")))


@pytest.mark.parametrize("gate", CLOSURE_GATES)
def test_gate_has_a_precommit_hook(gate: str) -> None:
    assert gate in _precommit_hook_ids(), (
        f"{gate} has no pre-commit hook: it would only be caught after the commit exists"
    )


@pytest.mark.parametrize("gate", CLOSURE_GATES)
def test_gate_has_a_ci_job_of_the_same_name(gate: str) -> None:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    assert gate in jobs, f"{gate} has no job in {WORKFLOW.name}"
    assert jobs[gate].get("name") == gate, (
        f"{gate}'s job display name must equal its id — the CI Summary L4 check resolves "
        f"the CONTEXT by that display name"
    )


@pytest.mark.parametrize("gate", CLOSURE_GATES)
def test_gate_is_asserted_by_ci_summary(gate: str) -> None:
    assert gate in _expected_external_contexts(), (
        f"{gate} is not in EXPECTED_EXTERNAL_CONTEXTS, so the required CI Summary umbrella "
        f"would report green with the gate absent — detection, not enforcement"
    )


def test_the_workflow_runs_on_pull_request_and_merge_group() -> None:
    """A gate that does not run on the merge SHA is not a merge gate."""
    triggers = _workflow()[True]  # YAML parses the `on:` key as the boolean True
    assert "pull_request" in triggers
    assert "merge_group" in triggers


def test_every_job_in_the_workflow_is_a_declared_closure_gate() -> None:
    """No unlisted job may ride this workflow without also being asserted at L4."""
    jobs = set(_workflow()["jobs"])
    assert jobs == set(CLOSURE_GATES), (
        f"jobs in {WORKFLOW.name} and the CLOSURE_GATES list have diverged: "
        f"{sorted(jobs ^ set(CLOSURE_GATES))}"
    )
