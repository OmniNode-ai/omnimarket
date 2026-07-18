# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the merge-check reason-code classifier (OMN-14765, epic OMN-14643).

Proves the classifier keys on jobs-API attempt facts (not ``gh pr checks`` text)
and returns exactly one typed reason code, RED-vs-correct against the naive
red-counting the friction report (F-07/F-08/F-09/F-10/F-23/F-25/F-26) documents.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.merge_control.reason_code_classifier import (
    EnumMergeCheckReasonCode,
    MergeCheckFacts,
    classify,
    classify_dict,
    dominant_reason_code,
    facts_from_job,
)

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "reason_codes"


def _fixture_files() -> list[Path]:
    files = sorted(_FIXTURE_DIR.glob("*.json"))
    assert files, f"no fixtures found under {_FIXTURE_DIR}"
    return files


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# 1. Fixture corpus — each real jobs-API payload classifies to its recorded code.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.stem)
def test_fixture_classifies_to_expected(path: Path) -> None:
    data = _load(path)
    expected = data["expected_reason_code"]
    got = classify_dict(data)
    assert str(got) == expected, (
        f"{path.name}: got {got}, expected {expected} — {data.get('description')}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.stem)
def test_fixture_red_vs_correct(path: Path) -> None:
    """RED-vs-correct: for every non-product incident the naive ``gh pr checks``
    path would count the check as a product red / dispatch a code fix, while the
    classifier returns a non-product typed code (rerun/withhold/refresh)."""
    data = _load(path)
    got = classify_dict(data)
    if data.get("naive_would_misclassify_as_product"):
        # The naive path (any non-success bucket -> product red) would be WRONG;
        # the classifier must NOT return product_failed for these.
        assert got is not EnumMergeCheckReasonCode.PRODUCT_FAILED, (
            f"{path.name}: classifier still reports product_failed for a "
            f"non-product incident — {data.get('description')}"
        )
    else:
        # Genuine product failures MUST be product_failed (not demoted to infra).
        assert got is EnumMergeCheckReasonCode.PRODUCT_FAILED, (
            f"{path.name}: a genuine product failure was demoted to {got}"
        )


@pytest.mark.unit
def test_corpus_covers_every_reason_code() -> None:
    seen = {str(classify_dict(_load(p))) for p in _fixture_files()}
    expected = {str(c) for c in EnumMergeCheckReasonCode}
    assert seen == expected, f"corpus missing reason codes: {expected - seen}"


# ---------------------------------------------------------------------------
# 2. Fail-closed default — indeterminate/unknown never becomes product_failed.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unattributed_failure_fails_closed_to_runner_infra() -> None:
    facts = MergeCheckFacts(
        job_conclusion="failure",
        failed_step_name="Enforce some custom gate",
        run_event="pull_request",
    )
    assert classify(facts) is EnumMergeCheckReasonCode.RUNNER_INFRA


@pytest.mark.unit
def test_unknown_conclusion_never_product() -> None:
    facts = MergeCheckFacts(job_conclusion="mystery", run_event="pull_request")
    assert classify(facts) is not EnumMergeCheckReasonCode.PRODUCT_FAILED
    assert classify(facts) is EnumMergeCheckReasonCode.RUNNER_INFRA


@pytest.mark.unit
def test_failure_with_no_step_fails_closed() -> None:
    facts = MergeCheckFacts(
        job_conclusion="failure", failed_step_name=None, run_event="pull_request"
    )
    assert classify(facts) is EnumMergeCheckReasonCode.RUNNER_INFRA


# ---------------------------------------------------------------------------
# 3. Precedence — STALE > OUTAGE > RUNNER_INFRA > CANCELLED > PRODUCT_FAILED.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stale_head_beats_product_failure() -> None:
    # Even a real pytest failure on an OLD head is stale, not a fix target.
    facts = MergeCheckFacts(
        job_conclusion="failure",
        failed_step_name="Run pytest",
        head_sha="dead",
        current_head_sha="beef",
        run_event="pull_request",
    )
    assert classify(facts) is EnumMergeCheckReasonCode.STALE_CONTEXT


@pytest.mark.unit
def test_superseded_beats_everything() -> None:
    facts = MergeCheckFacts(
        job_conclusion="failure",
        failed_step_name="Run ruff",
        is_superseded=True,
        run_event="pull_request",
    )
    assert classify(facts) is EnumMergeCheckReasonCode.STALE_CONTEXT


@pytest.mark.unit
def test_api_outage_beats_runner_infra_and_product() -> None:
    facts = MergeCheckFacts(
        job_conclusion="failure",
        failed_step_name="Run pytest",  # product-looking
        log_signatures=("503 service unavailable",),
        run_event="pull_request",
    )
    assert classify(facts) is EnumMergeCheckReasonCode.GITHUB_API_OUTAGE


@pytest.mark.unit
def test_runner_infra_hang_signature_beats_product_step() -> None:
    # F-23: a product-looking test step that actually hung (os._exit) is infra.
    facts = MergeCheckFacts(
        job_conclusion="failure",
        failed_step_name="Run tests",
        log_signatures=("os._exit(1)", "thread timeout"),
        run_event="pull_request",
    )
    assert classify(facts) is EnumMergeCheckReasonCode.RUNNER_INFRA


@pytest.mark.unit
def test_infra_step_cancellation_is_infra_not_cancelled() -> None:
    facts = MergeCheckFacts(
        job_conclusion="cancelled",
        failed_step_name="Run actions/checkout@v4",
        run_event="pull_request",
    )
    assert classify(facts) is EnumMergeCheckReasonCode.RUNNER_INFRA


@pytest.mark.unit
def test_timed_out_without_signature_is_cancelled() -> None:
    facts = MergeCheckFacts(
        job_conclusion="timed_out",
        failed_step_name=None,
        run_event="pull_request",
    )
    assert classify(facts) is EnumMergeCheckReasonCode.CANCELLED


@pytest.mark.unit
def test_non_pr_event_only_stale_when_required() -> None:
    # A non-PR-associated event on a REQUIRED context is stale.
    req = MergeCheckFacts(
        job_conclusion="failure",
        failed_step_name="Run pytest",
        run_event="workflow_dispatch",
        required_context=True,
    )
    assert classify(req) is EnumMergeCheckReasonCode.STALE_CONTEXT
    # On a NON-required context the event is not a staleness signal; the product
    # failure stands.
    non_req = MergeCheckFacts(
        job_conclusion="failure",
        failed_step_name="Run pytest",
        run_event="workflow_dispatch",
        required_context=False,
    )
    assert classify(non_req) is EnumMergeCheckReasonCode.PRODUCT_FAILED


# ---------------------------------------------------------------------------
# 4. facts_from_job — jobs-API job object extraction.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_facts_from_job_extracts_failed_step_and_attempt() -> None:
    job = {
        "run_id": 42,
        "run_attempt": 3,
        "status": "completed",
        "conclusion": "failure",
        "head_sha": "cafe",
        "steps": [
            {"name": "Set up job", "conclusion": "success", "number": 1},
            {"name": "Run mypy --strict", "conclusion": "failure", "number": 2},
        ],
    }
    facts = facts_from_job(job, run_event="pull_request", current_head_sha="cafe")
    assert facts.attempt == 3
    assert facts.run_id == "42"
    assert facts.failed_step_name == "Run mypy --strict"
    assert classify(facts) is EnumMergeCheckReasonCode.PRODUCT_FAILED


@pytest.mark.unit
def test_facts_from_job_no_failed_step_returns_none() -> None:
    job = {
        "run_id": 43,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "cancelled",
        "head_sha": "f00d",
        "steps": [
            {"name": "Set up job", "conclusion": "success", "number": 1},
        ],
    }
    facts = facts_from_job(job, run_event="pull_request", current_head_sha="f00d")
    assert facts.failed_step_name is None
    assert classify(facts) is EnumMergeCheckReasonCode.CANCELLED


# ---------------------------------------------------------------------------
# 5. dominant_reason_code — PR-level collapse precedence.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dominant_product_wins() -> None:
    codes = ("runner_infra", "cancelled", "product_failed")
    assert dominant_reason_code(codes) is EnumMergeCheckReasonCode.PRODUCT_FAILED


@pytest.mark.unit
def test_dominant_outage_beats_infra() -> None:
    codes = ("runner_infra", "github_api_outage", "cancelled")
    assert dominant_reason_code(codes) is EnumMergeCheckReasonCode.GITHUB_API_OUTAGE


@pytest.mark.unit
def test_dominant_infra_beats_cancelled() -> None:
    assert (
        dominant_reason_code(("cancelled", "runner_infra"))
        is EnumMergeCheckReasonCode.RUNNER_INFRA
    )


@pytest.mark.unit
def test_dominant_empty_is_none() -> None:
    assert dominant_reason_code(()) is None


# ---------------------------------------------------------------------------
# 6. The Rule-5 gate runner itself passes on the shipped corpus.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_gate_runner_passes_on_corpus() -> None:
    import importlib.util

    script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "ci"
        / "check_merge_reason_codes.py"
    )
    spec = importlib.util.spec_from_file_location("check_merge_reason_codes", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main([]) == 0
