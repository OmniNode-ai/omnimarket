# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""List the broker topics that were never produced, with evidence (OMN-23614).

2026-11-03 08:12Z on the broker read: 118 topics held no record at all (high watermark
total 0, nothing inside retention, no consumer group), the leftovers of abandoned
provisioning. Naming them by hand kept the ones a contract still declares and the ones
the offsets failed to read, so the listing stays a pure read: every topic carries the
evidence that listed it or names the reason it was not, and deletion is a separate
effect behind the operator's go.
"""

from __future__ import annotations

from omnimarket.events.topic_never_produced import (
    ModelNeverProducedReport,
    ModelNeverProducedRequest,
    ModelNeverProducedTopic,
    ModelNotListedTopic,
    ModelTopicEvidence,
)


def _never_produced_reason(
    topic: ModelTopicEvidence,
    protected_prefixes: tuple[str, ...],
    list_declared_producers: bool,
) -> str | None:
    """The reason the topic is not listed, or ``None`` when it is never produced."""
    if any(topic.name.startswith(prefix) for prefix in protected_prefixes):
        return "protected"
    if (
        topic.high_watermark_total is None
        or topic.records_in_retention is None
        or topic.consumer_groups is None
    ):
        return "evidence_unread"
    if topic.high_watermark_total > 0:
        return "produced"
    if topic.records_in_retention > 0:
        return "data_in_retention"
    if topic.consumer_groups:
        return "has_consumer_group"
    if topic.declared_producers and not list_declared_producers:
        return "declared_producer"
    return None


def _evidence(topic: ModelTopicEvidence) -> tuple[str, ...]:
    if topic.declared_producers:
        producers = ", ".join(sorted(topic.declared_producers))
        return (
            "no record was ever produced (high watermark total 0)",
            "no data inside retention",
            "no consumer group reads it",
            f"contract(s) declaring a producer: {producers}",
        )
    return (
        "no record was ever produced (high watermark total 0)",
        "no data inside retention",
        "no consumer group reads it",
        "no contract declares a producer",
    )


class HandlerTopicNeverProduced:
    def handle(self, request: ModelNeverProducedRequest) -> ModelNeverProducedReport:
        rule = request.rule
        listed: list[ModelNeverProducedTopic] = []
        not_listed: list[ModelNotListedTopic] = []
        for topic in request.topics:
            reason = _never_produced_reason(
                topic, rule.protected_prefixes, rule.list_declared_producers
            )
            if reason is None:
                listed.append(
                    ModelNeverProducedTopic(
                        name=topic.name,
                        partitions=topic.partitions,
                        evidence=_evidence(topic),
                    )
                )
            else:
                not_listed.append(ModelNotListedTopic(name=topic.name, reason=reason))
        return ModelNeverProducedReport(
            listed=tuple(sorted(listed, key=lambda entry: entry.name)),
            not_listed=tuple(sorted(not_listed, key=lambda entry: entry.name)),
            topics_read=len(request.topics),
        )
