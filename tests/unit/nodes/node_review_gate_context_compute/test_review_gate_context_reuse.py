# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Proves ONE reading: the landing decision's read_review_gate and the shared read_head_review agree.

For the same verdict rows, both give the same verdict for a new-work PR. The test also asserts that
review_gate.py imports read_head_review, by inspecting its source.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

from omnimarket.handlers.review_head_reading import read_head_review
from omnimarket.models.model_landing_review import (
    EnumReviewGateVerdict,
    ModelLandingReviewFacts,
    ModelLandingReviewVerdictRow,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingPolicy,
    ModelLandingPrFacts,
)
from omnimarket.nodes.node_pr_landing_decision_compute.review_gate import (
    read_review_gate,
)

pytestmark = pytest.mark.unit

HEAD = "7265930bafc673e66ced5bb67b9464bc460f1d77"
OLD_HEAD = "f7402aa63d1b74b082b174badbe07ac91dcddf36"
NOW = datetime(2026, 11, 15, 13, 0, 0, tzinfo=UTC)


def _row(head: str, verdict: str, at: datetime) -> ModelLandingReviewVerdictRow:
    return ModelLandingReviewVerdictRow(head_sha=head, verdict=verdict, at=at)


def _pr_facts(review: ModelLandingReviewFacts) -> ModelLandingPrFacts:
    return ModelLandingPrFacts(
        pr="example-org/example-repo#42",
        head_sha=HEAD,
        state="open",
        ci="green",
        merge_state="clean",
        created_at="2026-11-15T11:00:00Z",
        review=review,
    )


def _policy() -> ModelLandingPolicy:
    return ModelLandingPolicy(review_read_expected=True)


def test_same_verdict_rows_give_same_reading() -> None:
    rows = (_row(HEAD, "clean", NOW), _row(OLD_HEAD, "changes_needed", NOW))
    review = ModelLandingReviewFacts(new_work=True, verdicts=rows)
    shared = read_head_review(review, HEAD)
    landing = read_review_gate(_pr_facts(review), _policy())
    assert landing is not None
    assert landing.verdict is shared.verdict
    assert landing.verdict is EnumReviewGateVerdict.PASS


def test_review_gate_imports_read_head_review() -> None:
    import omnimarket.nodes.node_pr_landing_decision_compute.review_gate as review_gate_module

    src = inspect.getsource(review_gate_module)
    assert "read_head_review" in src
