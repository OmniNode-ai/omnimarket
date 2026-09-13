# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The OMN-18311 no-house-entry gate is ENFORCED, and this says exactly how.

Operating Rule 5: a detection tool that is not a pre-merge gate is advisory and
gets ignored. OMN-18311 AC4 makes that concrete for release criterion C12's
third clause, so the gate carries three surfaces at once and each is asserted
here rather than trusted from a PR body:

1. a **pre-commit hook** (``byok-catalogue-no-house-entry``), so it fires before
   the commit exists;
2. a **CI job** in its own workflow file, whose display name is a context
   another surface can point at BY NAME;
3. an entry in ``scripts/ci/ci_summary_gate.py::EXPECTED_EXTERNAL_CONTEXTS``.

Surface 3 is what actually blocks a merge here. ``omnimarket``'s ``dev``
protection does not list this context directly; the required context it rides is
the ``CI Summary`` umbrella, whose layer-4 check resolves every name in that
tuple against the head SHA's check-runs and fails closed on one that is missing,
still running, or not a passing conclusion. Same mechanism
``Routing Tier Bindability`` already uses, and why no branch-protection change
is needed.

The contrast that motivates the whole ticket is asserted at the bottom: the
sibling OMN-17353 suite has no hook and no named job, so it is enforced only
incidentally by the generic split ``test`` job collecting the whole ``tests/``
tree. That is exactly the shape AC4 forbids for this one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github/workflows/byok-catalogue-no-house-entry.yml"
PRECOMMIT = REPO_ROOT / ".pre-commit-config.yaml"
CI_SUMMARY_GATE = REPO_ROOT / "scripts/ci/ci_summary_gate.py"

#: The pre-commit hook id and the workflow job id.
GATE_ID = "byok-catalogue-no-house-entry"

#: The job's display name, which IS the CI Summary L4 context string.
GATE_CONTEXT = "BYOK Catalogue No House Entry"

#: The single entry point all three surfaces call, so a local pass and a CI pass
#: cannot disagree.
ENTRY_POINT = "omnimarket.validators.byok_catalogue_no_house_entry"


def _workflow() -> dict[str, object]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _hooks() -> list[dict[str, object]]:
    config = yaml.safe_load(PRECOMMIT.read_text(encoding="utf-8"))
    return [hook for repo in config.get("repos", []) for hook in repo.get("hooks", [])]


def _expected_external_contexts() -> tuple[str, ...]:
    """Read the tuple literal without importing the script (CI-only imports)."""
    source = CI_SUMMARY_GATE.read_text(encoding="utf-8")
    match = re.search(
        r"EXPECTED_EXTERNAL_CONTEXTS:\s*tuple\[str, \.\.\.\]\s*=\s*\((?P<body>.*?)\n\)",
        source,
        re.DOTALL,
    )
    assert match is not None, "EXPECTED_EXTERNAL_CONTEXTS tuple not found"
    return tuple(re.findall(r'"([^"]+)"', match.group("body")))


class TestTheGateHasAllThreeSurfaces:
    def test_a_precommit_hook_fires_before_the_commit_exists(self) -> None:
        hook = next((h for h in _hooks() if h.get("id") == GATE_ID), None)
        assert hook is not None, (
            f"{GATE_ID} has no pre-commit hook: the clause would only be caught "
            "after the commit exists"
        )
        assert ENTRY_POINT in str(hook["entry"]), hook["entry"]

    def test_the_hook_watches_both_contracts_the_predicate_reads(self) -> None:
        """A filter that misses either input lets a defect through locally.

        The predicate is a JOIN: an edit to the platform contract alone can turn
        a clean catalogue row into a house entry without the catalogue file
        changing at all.
        """
        hook = next(h for h in _hooks() if h.get("id") == GATE_ID)
        pattern = str(hook["files"])
        for path in (
            "src/omnimarket/configs/byok_provider_backends.v1.yaml",
            "src/omnimarket/configs/bifrost_delegation.yaml",
            "src/omnimarket/validators/byok_catalogue_no_house_entry.py",
        ):
            assert re.match(pattern, path), (path, pattern)

    def test_a_named_ci_job_runs_the_same_entry_point(self) -> None:
        jobs = _workflow()["jobs"]
        assert isinstance(jobs, dict)
        assert GATE_ID in jobs, f"{GATE_ID} has no job in {WORKFLOW.name}"
        job = jobs[GATE_ID]
        assert job.get("name") == GATE_CONTEXT, (
            "the job display name is the CI Summary L4 context; a rename here "
            "silently points that assertion at nothing"
        )
        run_steps = " ".join(str(s.get("run", "")) for s in job["steps"])
        assert ENTRY_POINT in run_steps, run_steps

    def test_the_ci_job_also_runs_the_falsifiability_proof(self) -> None:
        """The injection positive control travels with the gate.

        Without it a refactor that made the checker always-clean would pass
        every run, and a green gate that cannot fail is not a gate.
        """
        job = _workflow()["jobs"][GATE_ID]
        run_steps = " ".join(str(s.get("run", "")) for s in job["steps"])
        assert "tests/test_omn18311_catalogue_no_house_entry.py" in run_steps

    def test_ci_summary_asserts_the_context_so_the_umbrella_can_block(self) -> None:
        assert GATE_CONTEXT in _expected_external_contexts(), (
            f"{GATE_CONTEXT} is not in EXPECTED_EXTERNAL_CONTEXTS, so the "
            "required CI Summary umbrella would report green with the gate "
            "absent — detection, not enforcement"
        )

    def test_the_workflow_runs_on_pull_request_to_dev(self) -> None:
        triggers = _workflow()[True]  # YAML parses the `on:` key as boolean True
        assert isinstance(triggers, dict)
        assert "pull_request" in triggers
        assert "dev" in triggers["pull_request"]["branches"]

    def test_the_workflow_does_not_trigger_on_edited(self) -> None:
        """OMN-16171: the gate reads committed config, never the PR body.

        A gate that re-runs on an `edited` event invites the belief that editing
        the PR body can change its verdict.
        """
        triggers = _workflow()[True]
        assert "edited" not in triggers["pull_request"]["types"]


class TestTheContrastThisTicketExistsFor:
    def test_the_omn17353_suite_has_no_named_gate_of_its_own(self) -> None:
        """Recorded, not fixed here — it is why AC4 demands a NAMED gate.

        OMN-17353's parity suite is collected only by the generic split `test`
        job in ci.yml. Nothing points at it by name, so a change to that job's
        selection would drop it with no surface reporting the loss. If this ever
        starts failing because that suite gained its own hook, that is a good
        change: delete this assertion.
        """
        assert not any("omn17353" in str(h.get("entry", "")).lower() for h in _hooks())
