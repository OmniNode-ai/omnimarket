# SPDX-License-Identifier: MIT
"""OMN-21949 AC3: an independent approval covers the committed reducer model."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

MODEL_DIR = Path(__file__).resolve().parents[2] / "formal" / "ledger_reducer_judge"


@pytest.mark.unit
def test_review_records_approval_by_an_independent_lane() -> None:
    review = (MODEL_DIR / "REVIEW.txt").read_text()
    reviewer = re.search(r"^Reviewer lane:\s+(\S+)", review, re.MULTILINE)
    assert reviewer is not None, "The review must identify its reviewer lane"
    assert reviewer.group(1) not in {"ledger-l9-1102", "ledger-l9b-1102"}
    assert "not the 2026-10-04 reviewer" in review
    assert re.findall(r"^VERDICT: (.+)$", review, re.MULTILINE) == ["approved"]


@pytest.mark.unit
def test_review_identifies_the_committed_model_bytes() -> None:
    review = (MODEL_DIR / "REVIEW.txt").read_text()
    reviewed_sha = re.search(r"LedgerReducerJudge\.tla\s+sha256 ([0-9a-f]{64})", review)
    assert reviewed_sha is not None, "The review must identify the model it approved"
    model_sha = hashlib.sha256((MODEL_DIR / "LedgerReducerJudge.tla").read_bytes())
    assert reviewed_sha.group(1) == model_sha.hexdigest()
