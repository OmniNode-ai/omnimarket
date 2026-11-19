# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review finding classifier: a finding blocks a merge only for a failing check or a cited code line (OMN-25531).

A finding is BLOCKING when it names a failing check, or cites a file and a line with a defect. Every other
finding is ADVISORY, including one that asks for running proof (``installed=``, ``observed=``) or other evidence:
it never holds a merge and becomes a post-merge follow-up. The classifier is pure: no I/O, no clock, no model call.

Two injected breaks show the assertions can fail for real: a classifier that treats any finding that names a file
as blocking, and one that treats a finding that asks for running proof as blocking.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from omnimarket.handlers.review_finding_class import classify_finding
from omnimarket.models.model_landing_review import (
    EnumReviewFindingClass,
    ModelLandingReviewFinding,
)

pytestmark = pytest.mark.unit

BLOCKING = EnumReviewFindingClass.BLOCKING
ADVISORY = EnumReviewFindingClass.ADVISORY

FAILING_CHECK = ModelLandingReviewFinding(
    summary="the lab check fails", failing_check="lab-tests / run"
)
LINE_WITH_DEFECT = ModelLandingReviewFinding(
    summary="off by one",
    file="src/omnimarket/x.py",
    line=47,
    defect="the slice drops the last row",
)
FILE_NO_LINE = ModelLandingReviewFinding(
    summary="this module looks wrong",
    file="src/omnimarket/x.py",
    defect="reads the wrong key",
)
LINE_NO_DEFECT = ModelLandingReviewFinding(
    summary="look here", file="src/omnimarket/x.py", line=47
)
LINE_NO_FILE = ModelLandingReviewFinding(
    summary="line 47", line=47, defect="reads the wrong key"
)
INSTALLED_PROOF = ModelLandingReviewFinding(
    summary="no installed= read of the change on the running install"
)
OBSERVED_PROOF = ModelLandingReviewFinding(
    summary="add an observed= line showing the tick ran after the change"
)
EVIDENCE_ASK = ModelLandingReviewFinding(summary="please attach the lab run receipt")


def _table(
    classify: Callable[[ModelLandingReviewFinding], EnumReviewFindingClass],
) -> None:
    """The exact expectation of the classifier, one row per class of finding."""
    assert classify(FAILING_CHECK) is BLOCKING
    assert classify(LINE_WITH_DEFECT) is BLOCKING
    assert classify(FILE_NO_LINE) is ADVISORY
    assert classify(LINE_NO_DEFECT) is ADVISORY
    assert classify(LINE_NO_FILE) is ADVISORY
    assert classify(INSTALLED_PROOF) is ADVISORY
    assert classify(OBSERVED_PROOF) is ADVISORY
    assert classify(EVIDENCE_ASK) is ADVISORY


def test_a_finding_that_names_a_failing_check_is_blocking() -> None:
    assert classify_finding(FAILING_CHECK) is BLOCKING


def test_a_finding_that_cites_a_file_and_line_with_a_defect_is_blocking() -> None:
    assert classify_finding(LINE_WITH_DEFECT) is BLOCKING


def test_a_finding_that_names_a_file_with_no_line_is_advisory() -> None:
    assert classify_finding(FILE_NO_LINE) is ADVISORY


@pytest.mark.parametrize("finding", [LINE_NO_DEFECT, LINE_NO_FILE])
def test_a_citation_missing_its_file_or_its_defect_is_advisory(
    finding: ModelLandingReviewFinding,
) -> None:
    assert classify_finding(finding) is ADVISORY


@pytest.mark.parametrize("finding", [INSTALLED_PROOF, OBSERVED_PROOF])
def test_a_finding_that_asks_for_installed_or_observed_proof_is_advisory(
    finding: ModelLandingReviewFinding,
) -> None:
    assert classify_finding(finding) is ADVISORY


def test_a_finding_that_asks_for_other_evidence_is_advisory() -> None:
    assert classify_finding(EVIDENCE_ASK) is ADVISORY


def test_the_classifier_is_deterministic() -> None:
    assert [classify_finding(f) for f in (FAILING_CHECK, FILE_NO_LINE)] == [
        classify_finding(f) for f in (FAILING_CHECK, FILE_NO_LINE)
    ]


def test_the_finding_model_refuses_an_empty_summary_a_zero_line_and_an_unknown_key() -> (
    None
):
    with pytest.raises(ValidationError):
        ModelLandingReviewFinding(summary="")
    with pytest.raises(ValidationError):
        ModelLandingReviewFinding(summary="x", file="a.py", line=0, defect="d")
    with pytest.raises(ValidationError):
        ModelLandingReviewFinding.model_validate({"summary": "x", "severity": "high"})


def test_the_real_classifier_meets_the_whole_table() -> None:
    _table(classify_finding)


# -------------------------------------------------------------------------------------------- controls


def test_break_any_file_is_blocking_is_caught() -> None:
    def any_file_blocks(f: ModelLandingReviewFinding) -> EnumReviewFindingClass:
        if f.failing_check is not None or f.file is not None:
            return BLOCKING
        return ADVISORY

    with pytest.raises(AssertionError):
        _table(any_file_blocks)


def test_break_a_proof_ask_is_blocking_is_caught() -> None:
    def proof_ask_blocks(f: ModelLandingReviewFinding) -> EnumReviewFindingClass:
        if "installed=" in f.summary or "observed=" in f.summary:
            return BLOCKING
        return classify_finding(f)

    with pytest.raises(AssertionError):
        _table(proof_ask_blocks)
