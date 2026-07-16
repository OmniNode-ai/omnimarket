# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the Governance Readiness reason-code classifier (OMN-14646, WS3).

These tests are deliberately non-vacuous: each distinct fixture must produce a
*distinct* typed reason code, and the fail-closed cases prove that a skipped /
cancelled / absent subcheck or a stale-but-present evidence binding can never
yield ``READY`` (green-on-absence / green-on-wrong is rejected).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci.governance_readiness import (
    EnumGovernanceReasonCode,
    EnumSubcheckOutcome,
    GovernanceFacts,
    categorize_conclusion,
    classify,
    classify_dict,
)

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "ci" / "governance_readiness.py"
)


def _code(**facts: object) -> EnumGovernanceReasonCode:
    return classify(GovernanceFacts.from_dict(facts)).code


# --- Happy path -----------------------------------------------------------


@pytest.mark.unit
def test_all_green_present_receipt_is_ready() -> None:
    result = classify(
        GovernanceFacts.from_dict(
            {
                "product_conclusion": "success",
                "occ_conclusion": "success",
                "receipt_conclusion": "success",
                "deploy_conclusion": "success",
                "evidence_state": "present",
                "receipt_state": "pass",
            }
        )
    )
    assert result.code is EnumGovernanceReasonCode.READY
    assert result.ready is True


# --- Distinct reason codes for the three required evidence fixtures --------


@pytest.mark.unit
def test_missing_evidence_fixture_fires_evidence_missing() -> None:
    assert (
        _code(
            product_conclusion="success",
            occ_conclusion="failure",
            receipt_conclusion="success",
            deploy_conclusion="success",
            evidence_state="missing",
        )
        is EnumGovernanceReasonCode.EVIDENCE_MISSING
    )


@pytest.mark.unit
def test_stale_evidence_fixture_fires_evidence_stale() -> None:
    assert (
        _code(
            product_conclusion="success",
            occ_conclusion="failure",
            receipt_conclusion="success",
            deploy_conclusion="success",
            evidence_state="stale",
        )
        is EnumGovernanceReasonCode.EVIDENCE_STALE
    )


@pytest.mark.unit
def test_failed_receipt_fixture_fires_receipt_failed() -> None:
    assert (
        _code(
            product_conclusion="success",
            occ_conclusion="success",
            receipt_conclusion="failure",
            deploy_conclusion="success",
            evidence_state="present",
            receipt_state="fail",
        )
        is EnumGovernanceReasonCode.RECEIPT_FAILED
    )


@pytest.mark.unit
def test_three_evidence_fixtures_are_all_distinct() -> None:
    """The three required fixtures must not collapse to the same code."""
    missing = _code(
        product_conclusion="success", occ_conclusion="failure", evidence_state="missing"
    )
    stale = _code(
        product_conclusion="success", occ_conclusion="failure", evidence_state="stale"
    )
    failed = _code(
        product_conclusion="success",
        occ_conclusion="success",
        receipt_conclusion="failure",
        evidence_state="present",
        receipt_state="fail",
    )
    assert len({missing, stale, failed}) == 3


# --- Other reason codes ---------------------------------------------------


@pytest.mark.unit
def test_product_red_fires_product_not_green() -> None:
    assert (
        _code(product_conclusion="failure", occ_conclusion="success")
        is EnumGovernanceReasonCode.PRODUCT_NOT_GREEN
    )


@pytest.mark.unit
def test_policy_hold_fires_policy_held() -> None:
    assert (
        _code(
            product_conclusion="success",
            occ_conclusion="success",
            receipt_conclusion="success",
            deploy_conclusion="success",
            evidence_state="present",
            receipt_state="pass",
            policy_held=True,
        )
        is EnumGovernanceReasonCode.POLICY_HELD
    )


# --- Fail-closed: skipped / cancelled / absent subchecks ------------------


@pytest.mark.unit
@pytest.mark.parametrize("conclusion", ["skipped", "cancelled", "timed_out", ""])
def test_infra_or_absent_governance_subcheck_fails_closed(conclusion: str) -> None:
    """A skipped/cancelled/absent governance subcheck maps to RUNNER_INFRA,
    never a silent pass."""
    result = classify(
        GovernanceFacts.from_dict(
            {
                "product_conclusion": "success",
                "occ_conclusion": conclusion,
                "receipt_conclusion": "success",
                "deploy_conclusion": "success",
                "evidence_state": "present",
                "receipt_state": "pass",
            }
        )
    )
    assert result.code is EnumGovernanceReasonCode.RUNNER_INFRA
    assert result.ready is False


@pytest.mark.unit
def test_skipped_receipt_is_not_ready() -> None:
    """Green-on-absence rejection: a skipped receipt subcheck cannot be READY."""
    result = classify(
        GovernanceFacts.from_dict(
            {
                "product_conclusion": "success",
                "occ_conclusion": "success",
                "receipt_conclusion": "skipped",
                "deploy_conclusion": "success",
                "evidence_state": "present",
            }
        )
    )
    assert result.ready is False
    assert result.code is EnumGovernanceReasonCode.RUNNER_INFRA


@pytest.mark.unit
def test_stale_present_evidence_is_rejected_even_if_occ_passed() -> None:
    """Green-on-wrong rejection: evidence resolved (occ passed on a cached head)
    but bound to a superseded head must be EVIDENCE_STALE, never READY."""
    result = classify(
        GovernanceFacts.from_dict(
            {
                "product_conclusion": "success",
                "occ_conclusion": "success",
                "receipt_conclusion": "success",
                "deploy_conclusion": "success",
                "evidence_state": "stale",
                "receipt_state": "pass",
            }
        )
    )
    assert result.ready is False
    assert result.code is EnumGovernanceReasonCode.EVIDENCE_STALE


@pytest.mark.unit
def test_unrecognized_conclusion_fails_closed_to_infra() -> None:
    assert categorize_conclusion("banana") is EnumSubcheckOutcome.INFRA
    assert categorize_conclusion("SUCCESS") is EnumSubcheckOutcome.PASS
    assert categorize_conclusion(None) is EnumSubcheckOutcome.ABSENT


# --- Precedence ordering --------------------------------------------------


@pytest.mark.unit
def test_product_precedes_evidence() -> None:
    """A product failure dominates a simultaneous missing-evidence signal."""
    assert (
        _code(
            product_conclusion="failure",
            occ_conclusion="failure",
            evidence_state="missing",
        )
        is EnumGovernanceReasonCode.PRODUCT_NOT_GREEN
    )


@pytest.mark.unit
def test_infra_precedes_evidence_missing() -> None:
    """When occ is cancelled we cannot confirm evidence: RUNNER_INFRA, not a
    forged EVIDENCE_MISSING diagnosis."""
    assert (
        _code(
            product_conclusion="success",
            occ_conclusion="cancelled",
            evidence_state="missing",
        )
        is EnumGovernanceReasonCode.RUNNER_INFRA
    )


# --- CLI contract (the workflow shells into this) -------------------------


@pytest.mark.unit
def test_cli_classify_reports_reason_code_and_stays_green_report_only() -> None:
    facts = {
        "product_conclusion": "success",
        "occ_conclusion": "failure",
        "evidence_state": "missing",
    }
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "classify",
            "--facts-json",
            json.dumps(facts),
            "--report-only",
            "true",
        ],
        capture_output=True,
        text=True,
        cwd=_SCRIPT.parents[2],
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["reason_code"] == "EVIDENCE_MISSING"
    assert payload["ready"] is False


@pytest.mark.unit
def test_cli_enforcement_mode_exits_nonzero_on_not_ready() -> None:
    facts = {"product_conclusion": "failure"}
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "classify",
            "--facts-json",
            json.dumps(facts),
            "--report-only",
            "false",
        ],
        capture_output=True,
        text=True,
        cwd=_SCRIPT.parents[2],
    )
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["reason_code"] == "PRODUCT_NOT_GREEN"


@pytest.mark.unit
def test_classify_dict_roundtrips() -> None:
    out = classify_dict(
        {
            "product_conclusion": "success",
            "occ_conclusion": "success",
            "receipt_conclusion": "success",
            "deploy_conclusion": "success",
            "evidence_state": "present",
            "receipt_state": "pass",
        }
    )
    assert out["reason_code"] == "READY"
    assert out["ready"] is True
