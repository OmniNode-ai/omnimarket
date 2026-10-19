# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Record the cost of each provider attempt of a Hostile Review call (OMN-20910).

The effect reports every provider attempt as a ``started`` and a ``finished``
observation (``ModelLlmDelegationCallObservation``). A cost record is built
from those observations, at that boundary, and delivered the moment the
attempt is over. It does not wait for the effect's result, which a caller that
cancels the invocation never receives, so every attempt made before a
cancellation is already recorded when the cancellation lands. An attempt that
had left and never answered is recorded as ``cancelled`` by
:func:`record_cancelled_review_attempt`, from its ``started`` observation.

Only a call whose request names its review (``request.review``) is recorded;
every other call is untouched. Delivery never raises: the call's own work must
not fail because a cost record could not be sent.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.models.review_cost import ModelReviewCallCostRecorded
from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_observation import (
    ModelLlmDelegationCallObservation,
)

logger = logging.getLogger(__name__)

_CONTRACT = Path(__file__).parent.parent / "contract.yaml"
_COST_RECORDED_SUFFIX = (
    "review-call-cost-recorded.v1"  # onex-topic-allow: suffix used for contract lookup
)


def _cost_recorded_topic() -> str:
    matches = tuple(
        topic
        for topic in contract_publish_topics(_CONTRACT)
        if topic.endswith(_COST_RECORDED_SUFFIX)
    )
    if len(matches) != 1:
        raise RuntimeError(
            f"Contract {_CONTRACT} must declare exactly one event_bus.publish_topics "
            f"topic ending with {_COST_RECORDED_SUFFIX!r}; found {matches!r}."
        )
    return matches[0]


TOPIC_REVIEW_CALL_COST_RECORDED = _cost_recorded_topic()

#: The attempt numbers of the invocation in progress, 1-based, in the order its
#: provider attempts are made. The effect binds one per invocation; outside an
#: invocation every attempt is number 1.
attempt_counter: ContextVar[Iterator[int] | None] = ContextVar(
    "llm_delegation_attempt_counter", default=None
)


def next_attempt() -> int:
    """The number of the provider attempt about to be made."""
    counter = attempt_counter.get()
    return next(counter) if counter is not None else 1


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def cache_usage_figures(
    usage: Mapping[str, Any],
) -> tuple[int | None, int | None, int | None]:
    """(cache read, cache write 5 minute, cache write 1 hour) a provider reported.

    ``None`` is "no figure reported", never zero. Only Anthropic's native
    usage fields are read: ``cache_read_input_tokens`` and ``cache_creation``
    (split by TTL, or the undivided ``cache_creation_input_tokens``, read as 5
    minute). Their ``input_tokens`` excludes the cached part, which is what the
    cache hit ratio is defined over. An OpenAI-shaped ``cached_tokens`` is not
    read: its ``prompt_tokens`` includes the cached part, so it would not mean
    the same thing.
    """
    read = _count(usage.get("cache_read_input_tokens"))
    write_5m: int | None = None
    write_1h: int | None = None
    creation = usage.get("cache_creation")
    if isinstance(creation, Mapping):
        write_5m = _count(creation.get("ephemeral_5m_input_tokens"))
        write_1h = _count(creation.get("ephemeral_1h_input_tokens"))
    if write_5m is None and write_1h is None:
        write_5m = _count(usage.get("cache_creation_input_tokens"))
    return read, write_5m, write_1h


def _deliver(event: ModelReviewCallCostRecorded, event_publisher: Any) -> None:
    try:
        if event_publisher is not None:
            event_publisher.publish(TOPIC_REVIEW_CALL_COST_RECORDED, event)
            return
        from omnimarket.events.emit_effect_topic_publisher import (
            EmitEffectTopicPublisher,
        )

        EmitEffectTopicPublisher().publish(
            topic=TOPIC_REVIEW_CALL_COST_RECORDED,
            event_type="review.call.cost.recorded",
            payload=event.model_dump(mode="json"),
            event_id=str(event.event_id),
            correlation_id=event.provenance.review_run_id,
            partition_key=event.provenance.review_run_id,
        )
    except Exception as exc:  # a cost record must never fail the call
        logger.warning("review cost event %s not delivered: %s", event.event_id, exc)


def record_finished_attempt(
    request: ModelLlmDelegationCallRequest,
    endpoint_url: str,
    observation: ModelLlmDelegationCallObservation,
    event_publisher: Any = None,
) -> None:
    """Record the attempt a ``finished`` observation reports, if this is a review call."""
    provenance = request.review
    if provenance is None or observation.phase != "finished":
        return
    _deliver(
        ModelReviewCallCostRecorded.for_finished_attempt(
            call_id=request.request_id,
            attempt=observation.attempt,
            recorded_at=datetime.now(UTC),
            provenance=provenance,
            requested_model=request.model_id,
            served_model=observation.served_model_id,
            key_ref=request.secret_ref,
            endpoint_url=endpoint_url,
            success=bool(observation.success),
            failure_class=observation.failure_class,
            finish_reason=observation.finish_reason,
            tokens_in=observation.tokens_in,
            tokens_out=observation.tokens_out,
            cache_read_tokens=observation.cache_read_tokens,
            cache_write_5m_tokens=observation.cache_write_5m_tokens,
            cache_write_1h_tokens=observation.cache_write_1h_tokens,
            latency_ms=observation.latency_ms,
            cost_usd=observation.cost_usd,
            pricing_manifest_version=request.pricing_manifest_version,
        ),
        event_publisher,
    )


def record_cancelled_review_attempt(
    request: ModelLlmDelegationCallRequest,
    observation: ModelLlmDelegationCallObservation,
    *,
    elapsed_ms: int,
    event_publisher: Any = None,
) -> None:
    """Record the attempt a ``started`` observation reports and no ``finished`` followed.

    For the caller that ended the invocation (a deadline, a killed child) and
    holds the last observation the effect reported before it did. A projection
    replaces this record if the attempt's own ``finished`` record arrives.
    """
    provenance = request.review
    if provenance is None or observation.phase != "started":
        return
    _deliver(
        ModelReviewCallCostRecorded.for_cancelled_attempt(
            call_id=request.request_id,
            attempt=observation.attempt,
            recorded_at=datetime.now(UTC),
            provenance=provenance,
            requested_model=request.model_id,
            key_ref=request.secret_ref,
            endpoint_url=request.endpoint_ref,
            elapsed_ms=max(0, elapsed_ms),
            pricing_manifest_version=request.pricing_manifest_version,
        ),
        event_publisher,
    )


__all__ = [
    "TOPIC_REVIEW_CALL_COST_RECORDED",
    "attempt_counter",
    "cache_usage_figures",
    "next_attempt",
    "record_cancelled_review_attempt",
    "record_finished_attempt",
]
