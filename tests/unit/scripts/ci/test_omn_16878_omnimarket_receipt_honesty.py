# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16878 AC3: `receipt-honesty` must stay load-bearing on omnimarket.

The OMN-16876 enforcement census found `receipt-honesty.yml` running on every
omnimarket PR while being mechanically unable to block one — it sat in the
companion test file's `EXEMPT_CONTEXTS` with a "self-declared staged, not yet
promoted" reason, not a genuine unmet technical precondition (the workflow's
own header comment says only "Required-status-check name (when later
flipped)"). Per Operating Rule 5, detection not wired as a pre-merge gate is
advisory and gets ignored.

`receipt-honesty` is not itself in omnimarket's dev branch-protection
`required_status_checks` (verified live 2026-08-29) — `EXPECTED_EXTERNAL_CONTEXTS`
plus the "CI Summary" umbrella IS the surface that makes it block, exactly as
it already does for `contract-validation` and `deploy-gate / deploy-gate` in
this same tuple. A context missing from this tuple is silently unenforced with
no branch-protection signal that it is missing.

These tests pin the tuple membership and the producer-side properties that
make membership meaningful, mirroring the omnibase_infra
`test_omn_16878_enforcement_wiring.py` pattern for the same ticket.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.ci.ci_summary_gate import (
    EXPECTED_EXTERNAL_CONTEXTS,
    EXTERNAL_GOOD_CONCLUSIONS,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

CONTEXT = "receipt-honesty"
WORKFLOW_FILE = "receipt-honesty.yml"

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


def test_receipt_honesty_is_asserted_by_ci_summary() -> None:
    """Membership in the tuple is the ONLY thing making this context block —
    it is not independently present in dev branch-protection required_status_checks."""
    assert CONTEXT in EXPECTED_EXTERNAL_CONTEXTS, (
        f"{CONTEXT!r} left EXPECTED_EXTERNAL_CONTEXTS. It is not in dev's "
        "required_status_checks either, so removing it here means the "
        "producer runs on every PR while blocking nothing — the exact "
        "advisory-not-enforced shape OMN-16876/OMN-16878 closed."
    )


def test_receipt_honesty_is_not_also_exempt() -> None:
    """A context cannot be both asserted here and recorded as deliberately
    exempt in the companion test file — that would be a contradictory claim."""
    from tests.unit.scripts.ci.test_ci_summary_gate import EXEMPT_CONTEXTS

    assert (WORKFLOW_FILE, CONTEXT) not in EXEMPT_CONTEXTS, (
        f"({WORKFLOW_FILE!r}, {CONTEXT!r}) is in both EXPECTED_EXTERNAL_CONTEXTS "
        "and EXEMPT_CONTEXTS — the exemption reason is dead/contradicted now "
        "that this context is asserted."
    )


def test_receipt_honesty_producer_reports_on_pr_and_merge_group() -> None:
    """A required context that cannot report on a queue SHA wedges the queue,
    should omnimarket dev ever re-enable one."""
    triggers = _triggers(_workflow())
    assert "pull_request" in triggers, (
        f"{CONTEXT!r}: producer has no pull_request trigger, so branch "
        "protection / the CI Summary poller could never see it satisfied."
    )
    assert "merge_group" in triggers, (
        f"{CONTEXT!r}: producer has no merge_group trigger. Should a queue "
        "ever be enabled on this repo, an asserted context that never "
        "reports on the queue SHA wedges every merge."
    )


def test_receipt_honesty_producer_job_has_no_skip_path() -> None:
    """No `needs:` and no job-level `if:` means nothing upstream can skip it —
    a skipped producer is the classic silent-pass shape (OMN-15057 vector 5)."""
    jobs = _workflow()["jobs"]
    # The context name is the bare job key — no job-level `name:` override.
    job = jobs.get(CONTEXT)
    assert job is not None, (
        f"{WORKFLOW_FILE} has no job id {CONTEXT!r}; the check-run name that "
        "the CI Summary poller keys on would change."
    )
    if job.get("needs"):
        assert str(job.get("if", "")).strip() == "always()", (
            f"{WORKFLOW_FILE}:{CONTEXT} gained `needs:` without "
            "`if: always()`. GitHub's implicit job-level `if:` is success() "
            "over needs, so an upstream failure would SKIP this job."
        )


def test_skipped_is_not_a_good_external_conclusion() -> None:
    """The assertion layer must treat a skipped external context as a failure
    — belt and braces alongside the no-skip-path check above."""
    assert "skipped" not in EXTERNAL_GOOD_CONCLUSIONS, (
        "Admitting 'skipped' here would let a skipped receipt-honesty run "
        "satisfy an asserted L4 context."
    )
