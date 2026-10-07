# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20073 S6: PR admission requires repo evidence and no OCC companion.

The callers that minted, healed, waited on or verified a PR's OCC evidence
companion are deleted, and neither CI Summary nor the required-checks manifest
expects a context that needs one. ``OCC Emitter Golden Gate`` and ``ONEX Change
Control Schema Compatibility`` test this repository's own companion emitter
against a pinned onex_change_control checkout and read no PR companion, so
branch protection no longer requires them but CI Summary still enforces them.
"""

from pathlib import Path

import pytest
import yaml

from scripts.ci import ci_summary_gate as gate

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPANION_CONTEXTS = {
    "occ-preflight / eligibility",
    "call-reject-skip-token / occ-preflight / eligibility",
    "verify / verify",
}
EMITTER_CONTEXTS = {
    "OCC Emitter Golden Gate",
    "ONEX Change Control Schema Compatibility",
}
PROTECTION_OCC_CONTEXTS = {
    "occ-preflight / eligibility",
    "call-reject-skip-token / occ-preflight / eligibility",
    "OCC Emitter Golden Gate",
    "ONEX Change Control Schema Compatibility",
}


def test_occ_caller_workflows_are_deleted() -> None:
    workflows = REPO_ROOT / ".github" / "workflows"
    assert not [
        name
        for name in (
            "call-occ-preflight.yml",
            "call-occ-autobind.yml",
            "occ-autobind-mint-verify.yml",
            "occ-companion-merge-heal.yml",
            "occ-receipt-runner.yml",
            "call-receipt-gate.yml",
        )
        if (workflows / name).exists()
    ]


def test_ci_has_no_companion_merged_gate() -> None:
    jobs = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    name = "OCC Companion Merged Gate (OMN-15214)"
    assert "occ-companion-merged" not in jobs
    assert all(job.get("name") != name for job in jobs.values())
    assert name not in gate.STRICT_GATE_JOBS
    assert name not in gate.GATE_JOBS


def test_ci_summary_expects_no_companion_context() -> None:
    for event in ("pull_request", "merge_group", None):
        expected = gate.expected_external_contexts(event)
        assert not COMPANION_CONTEXTS.intersection(expected), event
        assert EMITTER_CONTEXTS.issubset(expected), event
        assert "call-reject-skip-token / scan / reject-skip-gate-token" in expected
        if event != "merge_group":
            assert "repo-evidence / dod-verify" in expected


def test_required_checks_manifest_names_no_occ_context() -> None:
    manifest = yaml.safe_load((REPO_ROOT / ".github/required-checks.yaml").read_text())
    names = {row["name"] for row in manifest["gates"]}
    assert not (PROTECTION_OCC_CONTEXTS | COMPANION_CONTEXTS).intersection(names)
