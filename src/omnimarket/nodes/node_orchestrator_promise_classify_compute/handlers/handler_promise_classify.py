# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Accept a delegated answer whole, or classify every source sentence by wording."""

from __future__ import annotations

import hashlib
import re

from pydantic import ValidationError

from omnimarket.handlers.operator_utterance import subject_terms
from omnimarket.models.model_orchestrator_promise import (
    ModelPromiseBarReport,
    ModelPromiseClassification,
    ModelPromiseClassifyRequest,
    ModelPromiseDelegateAnswer,
    ModelPromiseDelegateItem,
    ModelPromiseItem,
    ModelPromisePolicy,
)
from omnimarket.models.operator_capture import (
    EnumClassifierSource,
    ModelSentenceDisposition,
)


def policy_digest(policy: ModelPromisePolicy) -> str:
    """An activation flip does not change the classifier that was measured."""
    return hashlib.sha256(policy.model_dump_json(exclude={"mode"}).encode()).hexdigest()


def machine_reply(text: str, policy: ModelPromisePolicy) -> bool:
    head = text.lstrip().lower()
    return (
        not head
        or any(head.startswith(prefix.lower()) for prefix in policy.machine_prefixes)
        or any(
            re.search(pattern, text, re.IGNORECASE)
            for pattern in policy.machine_patterns
        )
    )


def _matches(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def bar_allows_activation(
    policy: ModelPromisePolicy, evaluation: ModelPromiseBarReport | None
) -> bool:
    if (
        evaluation is None
        or not evaluation.passed
        or evaluation.origin != "corpus"
        or evaluation.policy_digest != policy_digest(policy)
        or evaluation.replies < policy.minimum_replies
        or policy.minimum_precision is None
        or policy.minimum_recall is None
    ):
        return False
    precision = evaluation.precision
    recall = evaluation.recall
    if policy.bar_statistic == "lower_bound":
        return (
            precision.lower >= policy.minimum_precision
            and recall.lower >= policy.minimum_recall
        )
    return (
        precision.estimate is not None
        and recall.estimate is not None
        and precision.estimate >= policy.minimum_precision
        and recall.estimate >= policy.minimum_recall
    )


def _fallback(
    sentences: tuple[str, ...], policy: ModelPromisePolicy
) -> tuple[ModelPromiseDelegateItem, ...]:
    return tuple(
        ModelPromiseDelegateItem(
            sentence=index,
            kind="promise",
            quote=quote,
            subject=" ".join(subject_terms(quote, policy.subject_terms)),
            confidence=(
                policy.uncertain_confidence
                if uncertain
                else policy.heuristic_confidence
            ),
        )
        for index, sentence in enumerate(sentences, 1)
        for part in re.split(
            policy.commitment_separator_pattern, sentence, flags=re.IGNORECASE
        )
        if (quote := part.strip())
        if (uncertain := _matches(policy.uncertain_patterns, quote))
        or _matches(policy.promise_patterns, quote)
    )


def _delegated(
    response: str,
    sentences: tuple[str, ...],
    fallback: tuple[ModelPromiseDelegateItem, ...],
    policy: ModelPromisePolicy,
) -> tuple[ModelPromiseDelegateItem, ...]:
    answer = ModelPromiseDelegateAnswer.model_validate_json(response)
    # An omitted, invented, duplicated or reordered sentence invalidates the WHOLE answer.
    if tuple(item.sentence for item in answer.items) != tuple(
        range(1, len(sentences) + 1)
    ):
        raise ValueError("delegated answer does not cover every sentence exactly once")
    if any(item.quote != sentences[item.sentence - 1] for item in answer.items):
        raise ValueError("delegated quote is not the exact source sentence")
    # A model cannot silently turn explicit commitment wording into chat.
    promises = {item.sentence for item in fallback}
    if any(
        item.sentence in promises
        and item.kind == "chat"
        and item.confidence >= policy.confidence_floor
        for item in answer.items
    ):
        raise ValueError("delegated chat would exclude commitment wording")
    if any(
        sum(value.sentence == item.sentence for value in fallback) > 1
        for item in answer.items
    ):
        raise ValueError(
            "delegated answer collapses multiple commitments in one sentence"
        )
    return tuple(
        item if item.kind == "promise" else item.model_copy(update={"kind": "promise"})
        for item in answer.items
        if item.kind == "promise" or item.confidence < policy.confidence_floor
    )


class HandlerPromiseClassify:
    """Pure, replay-stable classification; confidence controls review, never exclusion."""

    def handle(
        self, request: ModelPromiseClassifyRequest
    ) -> ModelPromiseClassification:
        utterance, policy = request.utterance, request.policy
        owner = policy.session_lanes.get(utterance.session_id, utterance.lane)
        base = {
            "capture_id": utterance.capture_id,
            "policy_digest": policy_digest(policy),
        }
        if (
            not owner
            or re.search(policy.orchestrator_lane_pattern, owner) is None
            or machine_reply(utterance.text, policy)
        ):
            return ModelPromiseClassification(
                **base,
                classifier=EnumClassifierSource.HEURISTIC,
                reason="not an orchestrator-authored reply",
                machine=True,
            )
        sentences = tuple(
            quote
            for match in re.finditer(policy.sentence_pattern, utterance.text)
            if (quote := match.group(0).strip())
        )
        classified = _fallback(sentences, policy)
        source = EnumClassifierSource.HEURISTIC
        reason = "fallback: " + (request.delegated_error or "no delegated answer")
        if request.delegated_response is not None:
            try:
                classified = _delegated(
                    request.delegated_response, sentences, classified, policy
                )
            except (ValidationError, ValueError) as exc:
                # Do not echo a model's invented words or malformed payload into a row.
                reason = f"fallback: delegated answer rejected ({type(exc).__name__})"
            else:
                source = EnumClassifierSource.DELEGATED
                reason = "delegated answer accepted whole"
        active = policy.mode == "active" and bar_allows_activation(
            policy, request.evaluation
        )
        items = tuple(
            ModelPromiseItem(
                item_id="promise-"
                + hashlib.sha256(
                    f"{utterance.capture_id}\0{item.sentence}\0{ordinal}\0{item.quote}".encode()
                ).hexdigest()[: policy.id_chars],
                sentence=item.sentence,
                quote=item.quote,
                subject=item.subject,
                confidence=item.confidence,
                state="open"
                if active and item.confidence >= policy.confidence_floor
                else "candidate",
                owner=owner or policy.default_owner,
            )
            for ordinal, item in enumerate(classified, 1)
        )
        by_sentence: dict[int, str] = {}
        for item in items:
            by_sentence.setdefault(item.sentence, item.item_id)
        return ModelPromiseClassification(
            **base,
            classifier=source,
            classifier_model=request.delegated_model
            if source is EnumClassifierSource.DELEGATED
            else None,
            reason=reason,
            items=items,
            low_confidence=sum(
                item.confidence < policy.confidence_floor for item in items
            ),
            dispositions=tuple(
                ModelSentenceDisposition(
                    sentence=index, quote=quote, item_id=by_sentence.get(index)
                )
                for index, quote in enumerate(sentences, 1)
            ),
        )
