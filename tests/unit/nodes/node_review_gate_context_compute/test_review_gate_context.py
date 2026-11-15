# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review-gate context decision: golden cases, error cases, and three injected breaks.

The head passes only on a CLEAN review verdict recorded for that exact head, or when the pull request belongs to a
declared bypass class; every other head is a failure with a named reason. The newest on-head verdict wins in both
orders. Three injected breaks show the assertions can fail for real: a judge that counts a CLEAN verdict on any head,
a judge that bypasses every pull request, and a judge that reads no verdict as a pass. Each break test asserts the
control (unbroken) result differs, so the red is the break's.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from omnimarket.handlers.review_head_reading import read_head_review
from omnimarket.models.model_landing_review import (
    EnumReviewGateVerdict,
    EnumReviewVerdict,
    ModelLandingReviewFacts,
    ModelLandingReviewVerdictRow,
    ModelReviewGateReading,
)
from omnimarket.models.model_review_gate_context import (
    EnumReviewContextReason,
    EnumReviewContextState,
    ModelReviewBypassClass,
    ModelReviewGateContextDecision,
    ModelReviewGateContextPolicy,
    ModelReviewGateContextRequest,
    ModelReviewGatePrSubject,
)
from omnimarket.nodes.node_review_gate_context_compute.handlers import (
    handler_review_gate_context as handler_module,
)
from omnimarket.nodes.node_review_gate_context_compute.handlers.handler_review_gate_context import (
    decide_review_gate_context,
)

pytestmark = pytest.mark.unit

HEAD = "7265930bafc673e66ced5bb67b9464bc460f1d77"
OLD_HEAD = "f7402aa63d1b74b082b174badbe07ac91dcddf36"
NOW = datetime(2026, 11, 15, 13, 0, 0, tzinfo=UTC)

POLICY = ModelReviewGateContextPolicy(
    check_name="fable-review-gate",
    bypass_classes=(
        ModelReviewBypassClass(
            name="bot_release_pull_request",
            why="A release pull request cut by the release automation; the release is reviewed at its tag",
            author_logins=("example-release-bot[bot]",),
            head_ref_pattern="automation/release-.*",
        ),
        ModelReviewBypassClass(
            name="bot_pin_bump_pull_request",
            why="A dependency pin bump cut by the release automation; the bump is reviewed at its tag",
            author_logins=("example-release-bot[bot]",),
            head_ref_pattern="automation/bump-.*",
            title_pattern="chore.*bump.*",
        ),
    ),
    title_clean="Review verdict CLEAN on head {head}",
    title_bypass="Bypass class {bypass_class}: no review verdict needed",
    title_missing="No review verdict recorded for head {head}",
    title_changes_needed="Review changes needed on head {head}",
    title_superseded="Review verdicts exist only for an older head than {head}",
)


def _row(head: str, verdict: str, at: datetime) -> ModelLandingReviewVerdictRow:
    return ModelLandingReviewVerdictRow(head_sha=head, verdict=verdict, at=at)


def _review(*rows: ModelLandingReviewVerdictRow) -> ModelLandingReviewFacts:
    return ModelLandingReviewFacts(new_work=True, verdicts=rows)


def _subject(
    review: ModelLandingReviewFacts | None = None,
    author: str = "example-dev",
    head_ref: str = "feature/x",
    title: str = "feat: something",
) -> ModelReviewGatePrSubject:
    return ModelReviewGatePrSubject(
        repository="example-org/example-repo",
        pr_number=42,
        head_sha=HEAD,
        author_login=author,
        head_ref=head_ref,
        title=title,
        review=review,
    )


def _request(
    review: ModelLandingReviewFacts | None = None,
    author: str = "example-dev",
    head_ref: str = "feature/x",
    title: str = "feat: something",
) -> ModelReviewGateContextRequest:
    return ModelReviewGateContextRequest(
        subject=_subject(review, author, head_ref, title), policy=POLICY
    )


def _decide(
    review: ModelLandingReviewFacts | None = None,
    author: str = "example-dev",
    head_ref: str = "feature/x",
    title: str = "feat: something",
) -> ModelReviewGateContextDecision:
    return decide_review_gate_context(_request(review, author, head_ref, title))


# ---------------------------------------------------------------------------
# Golden cases
# ---------------------------------------------------------------------------


def test_clean_verdict_on_head_passes() -> None:
    d = _decide(_review(_row(HEAD, "clean", NOW)))
    assert d.state is EnumReviewContextState.SUCCESS
    assert d.reason is EnumReviewContextReason.CLEAN_ON_HEAD
    assert d.bypass_class is None
    assert d.title == f"Review verdict CLEAN on head {HEAD[:12]}"
    assert d.summary == f"review CLEAN on the head {HEAD[:12]}"


def test_bypass_class_passes_with_name_and_why() -> None:
    d = _decide(
        None,
        author="example-release-bot[bot]",
        head_ref="automation/release-1.2.3",
    )
    assert d.state is EnumReviewContextState.SUCCESS
    assert d.reason is EnumReviewContextReason.BYPASS_CLASS
    assert d.bypass_class == "bot_release_pull_request"
    assert "A release pull request" in d.summary
    assert d.title == "Bypass class bot_release_pull_request: no review verdict needed"


def test_newest_on_head_verdict_wins_clean_then_changes() -> None:
    d = _decide(_review(_row(HEAD, "changes_needed", NOW), _row(HEAD, "clean", NOW)))
    assert d.reason is EnumReviewContextReason.CLEAN_ON_HEAD


def test_newest_on_head_verdict_wins_changes_then_clean() -> None:
    d = _decide(_review(_row(HEAD, "clean", NOW), _row(HEAD, "changes_needed", NOW)))
    assert d.reason is EnumReviewContextReason.REVIEW_CHANGES_NEEDED


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_no_verdict_is_review_missing() -> None:
    d = _decide(_review())
    assert d.state is EnumReviewContextState.FAILURE
    assert d.reason is EnumReviewContextReason.REVIEW_MISSING


def test_changes_needed_on_head_is_review_changes_needed() -> None:
    d = _decide(_review(_row(HEAD, "changes_needed", NOW)))
    assert d.state is EnumReviewContextState.FAILURE
    assert d.reason is EnumReviewContextReason.REVIEW_CHANGES_NEEDED


def test_clean_verdict_on_older_head_only_is_review_superseded() -> None:
    d = _decide(_review(_row(OLD_HEAD, "clean", NOW)))
    assert d.state is EnumReviewContextState.FAILURE
    assert d.reason is EnumReviewContextReason.REVIEW_SUPERSEDED


def test_review_none_is_review_missing() -> None:
    d = _decide(None)
    assert d.state is EnumReviewContextState.FAILURE
    assert d.reason is EnumReviewContextReason.REVIEW_MISSING


def test_bypass_author_with_non_matching_head_ref_is_not_bypassed() -> None:
    d = _decide(
        _review(_row(HEAD, "changes_needed", NOW)),
        author="example-release-bot[bot]",
        head_ref="feature/x",
    )
    assert d.reason is EnumReviewContextReason.REVIEW_CHANGES_NEEDED


def test_author_compared_case_insensitively() -> None:
    d = _decide(
        None,
        author="Example-Release-Bot[bot]",
        head_ref="automation/release-1.2.3",
    )
    assert d.reason is EnumReviewContextReason.BYPASS_CLASS
    assert d.bypass_class == "bot_release_pull_request"


def test_unknown_input_key_refused_by_model() -> None:
    with pytest.raises(ValidationError):
        ModelReviewGatePrSubject(
            repository="example-org/example-repo",
            pr_number=42,
            head_sha=HEAD,
            author_login="example-dev",
            head_ref="feature/x",
            title="feat: something",
            unknown_key="x",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Injected breaks: each asserts the control (unbroken) result differs
# ---------------------------------------------------------------------------


def test_break_clean_verdict_on_any_head(monkeypatch: pytest.MonkeyPatch) -> None:
    control = _decide(_review(_row(OLD_HEAD, "clean", NOW)))
    assert control.reason is EnumReviewContextReason.REVIEW_SUPERSEDED

    def _broken(review, head_sha):
        if review is not None and any(
            r.verdict is EnumReviewVerdict.CLEAN for r in review.verdicts
        ):
            return ModelReviewGateReading(
                verdict=EnumReviewGateVerdict.PASS,
                detail=f"review CLEAN on the head {head_sha[:12]}",
            )
        return read_head_review(review, head_sha)

    monkeypatch.setattr(handler_module, "read_head_review", _broken)
    broken = _decide(_review(_row(OLD_HEAD, "clean", NOW)))
    assert broken.reason is EnumReviewContextReason.CLEAN_ON_HEAD
    assert broken.reason != control.reason


def test_break_bypasses_every_pull_request(monkeypatch: pytest.MonkeyPatch) -> None:
    control = _decide(_review(_row(HEAD, "changes_needed", NOW)))
    assert control.reason is EnumReviewContextReason.REVIEW_CHANGES_NEEDED

    def _broken_matches(cls, subject):
        return True

    monkeypatch.setattr(handler_module, "_matches", _broken_matches)
    broken = _decide(_review(_row(HEAD, "changes_needed", NOW)))
    assert broken.reason is EnumReviewContextReason.BYPASS_CLASS
    assert broken.reason != control.reason


def test_break_reads_no_verdict_as_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    control = _decide(None)
    assert control.reason is EnumReviewContextReason.REVIEW_MISSING

    def _broken(review, head_sha):
        if review is None:
            return ModelReviewGateReading(
                verdict=EnumReviewGateVerdict.PASS,
                detail=f"review CLEAN on the head {head_sha[:12]}",
            )
        return read_head_review(review, head_sha)

    monkeypatch.setattr(handler_module, "read_head_review", _broken)
    broken = _decide(None)
    assert broken.reason is EnumReviewContextReason.CLEAN_ON_HEAD
    assert broken.reason != control.reason
