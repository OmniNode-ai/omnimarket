# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18796 (epic OMN-18775): pin the advisory-job-gate registration.

Hostile Reviewer findings on PR #2669 (both models, below quorum) flagged
that the diff registers a new context in `EXPECTED_EXTERNAL_CONTEXTS` with no
test proving the registration. Confirmed RED against the merge base before
this ticket's change (`grep -c "advisory-job-gate / advisory-job-gate"
scripts/ci/ci_summary_gate.py` at merge base `99521e45d45126a284416b0b7fc32
446fafb144c` returns 0) and GREEN on this branch (returns 1) — the same proof
`test_omn_16878_omnimarket_receipt_honesty.py` pins for its own single-entry
registration, followed here for the new entry.

Unlike `receipt-honesty.yml`, `advisory-job-gate.yml` triggers on
`pull_request` only — this repo carries no merge queue on `dev`
(`mergeQueue: null`, live-verified), so there is no `merge_group` trigger to
pin here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.ci.ci_summary_gate import EXPECTED_EXTERNAL_CONTEXTS

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

CONTEXT = "advisory-job-gate / advisory-job-gate"
WORKFLOW_FILE = "advisory-job-gate.yml"
JOB_ID = "advisory-job-gate"

pytestmark = pytest.mark.unit


def _workflow() -> dict:
    return yaml.safe_load((WORKFLOWS_DIR / WORKFLOW_FILE).read_text())


def _triggers(workflow: dict) -> set[str]:
    # PyYAML parses a bare `on:` key as the boolean True.
    raw = workflow.get(True, workflow.get("on"))
    if isinstance(raw, dict):
        return set(raw)
    if isinstance(raw, list):
        return set(raw)
    return {str(raw)}


def test_advisory_job_gate_is_asserted_by_ci_summary() -> None:
    """Membership in the tuple is the ONLY thing making this context block —
    the reusable caller has no in-run poller coverage of its own."""
    assert CONTEXT in EXPECTED_EXTERNAL_CONTEXTS, (
        f"{CONTEXT!r} left EXPECTED_EXTERNAL_CONTEXTS. Removing it means the "
        "advisory-job-gate producer runs on every PR while blocking "
        "nothing — the exact advisory-not-enforced shape Operating Rule 5 "
        "describes."
    )


def test_advisory_job_gate_is_not_also_exempt() -> None:
    """A context cannot be both asserted here and recorded as deliberately
    exempt in the companion test file — that would be a contradictory claim."""
    from tests.unit.scripts.ci.test_ci_summary_gate import EXEMPT_CONTEXTS

    assert (WORKFLOW_FILE, JOB_ID) not in EXEMPT_CONTEXTS, (
        f"({WORKFLOW_FILE!r}, {JOB_ID!r}) is in both EXPECTED_EXTERNAL_CONTEXTS "
        "and EXEMPT_CONTEXTS — the exemption reason would be dead/contradicted "
        "while this context is asserted."
    )


def test_advisory_job_gate_producer_has_pull_request_trigger_and_no_paths_filter() -> (
    None
):
    """An asserted (L4) context must always produce a check-run. A
    `pull_request.paths` filter would make it legitimately absent on some PR
    shapes, which is the permanent PENDING->FAILURE wedge the header comment
    in advisory-job-gate.yml documents choosing not to risk."""
    workflow = _workflow()
    triggers = _triggers(workflow)
    assert "pull_request" in triggers, (
        f"{CONTEXT!r}: producer has no pull_request trigger, so branch "
        "protection / the CI Summary poller could never see it satisfied."
    )
    raw_on = workflow.get(True, workflow.get("on"))
    pr_cfg = raw_on.get("pull_request") if isinstance(raw_on, dict) else None
    assert not (isinstance(pr_cfg, dict) and "paths" in pr_cfg), (
        f"{WORKFLOW_FILE} gained a pull_request.paths filter — this context "
        "is asserted (L4) and must fire unconditionally; a filtered PR shape "
        "would wedge every PR that does not touch the filtered paths."
    )


def test_advisory_job_gate_producer_job_has_no_skip_path() -> None:
    """No `needs:` on the caller job means nothing upstream can skip it — a
    skipped producer is the classic silent-pass shape (OMN-15057 vector 5)."""
    jobs = _workflow()["jobs"]
    job = jobs.get(JOB_ID)
    assert job is not None, (
        f"{WORKFLOW_FILE} has no job id {JOB_ID!r}; the check-run name the "
        "CI Summary poller keys on (via ` / `-joined caller/inner names) "
        "would change."
    )
    assert not job.get("needs"), (
        f"{WORKFLOW_FILE}:{JOB_ID} gained `needs:` — verify the implicit "
        "job-level `if:` (success() over needs) cannot SKIP this job before "
        "relying on that, since a skipped required context reads as passing."
    )
