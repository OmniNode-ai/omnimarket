# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review-gate context decision: the pure verdict of a required review check for one pull request head.

``decide_review_gate_context`` asks, in order: does the pull request belong to a declared bypass class (in declared
order)? If so it passes with that class's name and its declared justification. Otherwise it reads the head's review
verdicts with the shared ``read_head_review``: a CLEAN verdict on the exact head passes, and a missing, changes-needed
or superseded reading fails with its named reason. No "new work" flag is read here: every pull request is gated
unless a bypass class matches. No clock and no I/O.
"""

from __future__ import annotations

import re

from omnimarket.handlers.review_head_reading import read_head_review
from omnimarket.models.model_landing_review import EnumReviewGateVerdict
from omnimarket.models.model_review_gate_context import (
    EnumReviewContextReason,
    EnumReviewContextState,
    ModelReviewBypassClass,
    ModelReviewGateContextDecision,
    ModelReviewGateContextRequest,
    ModelReviewGatePrSubject,
)


def _fill(title: str, head: str, bypass_class: str | None) -> str:
    """Fill the two tokens of a policy title without ever formatting untrusted text."""
    return title.replace("{head}", head).replace("{bypass_class}", bypass_class or "")


def _matches(cls: ModelReviewBypassClass, subject: ModelReviewGatePrSubject) -> bool:
    """A class matches when the author is declared and every given pattern full-matches the subject."""
    if subject.author_login.lower() not in {a.lower() for a in cls.author_logins}:
        return False
    if cls.head_ref_pattern is not None and not re.fullmatch(
        cls.head_ref_pattern, subject.head_ref
    ):
        return False
    if cls.title_pattern is not None and not re.fullmatch(
        cls.title_pattern, subject.title
    ):
        return False
    return True


def decide_review_gate_context(
    request: ModelReviewGateContextRequest,
) -> ModelReviewGateContextDecision:
    """The verdict of the review-gate context check for one pull request head."""
    subject = request.subject
    policy = request.policy
    head = subject.head_sha[:12]
    for cls in policy.bypass_classes:
        if _matches(cls, subject):
            return ModelReviewGateContextDecision(
                repository=subject.repository,
                pr_number=subject.pr_number,
                head_sha=subject.head_sha,
                check_name=policy.check_name,
                state=EnumReviewContextState.SUCCESS,
                reason=EnumReviewContextReason.BYPASS_CLASS,
                bypass_class=cls.name,
                title=_fill(policy.title_bypass, head, cls.name),
                summary=f"{cls.why}: {_fill(policy.title_bypass, head, cls.name)}",
            )
    reading = read_head_review(subject.review, subject.head_sha)
    if reading.verdict is EnumReviewGateVerdict.PASS:
        return ModelReviewGateContextDecision(
            repository=subject.repository,
            pr_number=subject.pr_number,
            head_sha=subject.head_sha,
            check_name=policy.check_name,
            state=EnumReviewContextState.SUCCESS,
            reason=EnumReviewContextReason.CLEAN_ON_HEAD,
            bypass_class=None,
            title=_fill(policy.title_clean, head, None),
            summary=reading.detail,
        )
    if reading.verdict is EnumReviewGateVerdict.MISSING:
        reason = EnumReviewContextReason.REVIEW_MISSING
        title = policy.title_missing
    elif reading.verdict is EnumReviewGateVerdict.CHANGES_NEEDED:
        reason = EnumReviewContextReason.REVIEW_CHANGES_NEEDED
        title = policy.title_changes_needed
    else:
        reason = EnumReviewContextReason.REVIEW_SUPERSEDED
        title = policy.title_superseded
    return ModelReviewGateContextDecision(
        repository=subject.repository,
        pr_number=subject.pr_number,
        head_sha=subject.head_sha,
        check_name=policy.check_name,
        state=EnumReviewContextState.FAILURE,
        reason=reason,
        bypass_class=None,
        title=_fill(title, head, None),
        summary=reading.detail,
    )


class HandlerReviewGateContext:
    """The ONEX handler of the review-gate context compute node."""

    def handle(
        self, request: ModelReviewGateContextRequest
    ) -> ModelReviewGateContextDecision:
        return decide_review_gate_context(request)


__all__ = ["HandlerReviewGateContext", "decide_review_gate_context"]
