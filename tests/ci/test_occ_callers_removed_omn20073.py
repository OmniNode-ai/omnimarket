# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20073 S6: PR admission requires repo evidence without OCC callers."""

from pathlib import Path

import pytest
import yaml

from scripts.ci import ci_summary_gate as gate

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
OCC_CONTEXTS = {
    "occ-preflight / eligibility",
    "OCC Emitter Golden Gate",
    "ONEX Change Control Schema Compatibility",
    "call-reject-skip-token / occ-preflight / eligibility",
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


def test_ci_summary_expects_no_occ_context() -> None:
    for event in ("pull_request", "merge_group", None):
        expected = gate.expected_external_contexts(event)
        assert not OCC_CONTEXTS.intersection(expected), event
        assert "call-reject-skip-token / scan / reject-skip-gate-token" in expected
        if event != "merge_group":
            assert "repo-evidence / dod-verify" in expected


def test_required_checks_manifest_names_no_occ_context() -> None:
    manifest = yaml.safe_load((REPO_ROOT / ".github/required-checks.yaml").read_text())
    assert not OCC_CONTEXTS.intersection(row["name"] for row in manifest["gates"])
