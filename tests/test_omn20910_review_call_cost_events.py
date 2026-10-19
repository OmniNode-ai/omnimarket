# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20910: one cost event per provider attempt of a Hostile Review call.

The record is made at the delegation call effect's per-attempt observation
boundary, so an invocation that is cancelled after some attempts still leaves
one record for each attempt it made, and a call that is not a review records
nothing. Fakes only, no network.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest

from omnimarket.enums.enum_review_call_outcome import EnumReviewCallOutcome
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.models.review_cost import (
    ModelReviewCallCostRecorded,
    ModelReviewCallProvenance,
    review_call_cost_event_id,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call as effect,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_observation import (
    ModelLlmDelegationCallObservation,
)

pytestmark = pytest.mark.unit

ENDPOINT = "http://review-gateway.example.test:8140/v1/chat/completions"
MODEL = "claude-sonnet-5-5"


class Publisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    def publish(self, topic: str, payload: Any) -> None:
        self.published.append((topic, payload))

    def cost_events(self) -> list[ModelReviewCallCostRecorded]:
        return [
            payload
            for topic, payload in self.published
            if topic == effect.TOPIC_REVIEW_CALL_COST_RECORDED
        ]


def _provenance() -> ModelReviewCallProvenance:
    return ModelReviewCallProvenance(
        review_run_id="run-1",
        repository="OmniNode-ai/omnimarket",
        pr_number=4187,
        head_sha="e448de5",
        voter_id="sonnet",
        persona_id="correctness",
        catalog_version="c1",
        prompt_version="2.0.0",
    )


def _request(
    *, review: ModelReviewCallProvenance | None = None
) -> ModelLlmDelegationCallRequest:
    return ModelLlmDelegationCallRequest(
        request_id=str(uuid4()),
        correlation_id=str(uuid4()),
        causation_id=str(uuid4()),
        model_id=MODEL,
        endpoint_ref=ENDPOINT,
        prompt="review this diff",
        prompt_hash="0" * 64,
        timeout_seconds=5,
        model_tier="cloud",
        provider="anthropic",
        pricing_manifest_version="pm-1",
        review=review,
    )


def _body(
    *, finish: str = "stop", usage: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": "{}"}, "finish_reason": finish}],
        "usage": usage
        or {
            "prompt_tokens": 1200,
            "completion_tokens": 300,
            "cache_read_input_tokens": 900,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 100,
                "ephemeral_1h_input_tokens": 50,
            },
        },
    }


class Provider:
    """One scripted answer per POST: a body, an HTTP status, or an exception."""

    def __init__(self, script: list[Any]) -> None:
        self.script = iter(script)

    def post(self, *, endpoint_url: str, **_: Any) -> transport.ModelTransportResponse:
        step = next(self.script)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, int):
            response = httpx.Response(
                step,
                request=httpx.Request("POST", endpoint_url),
                json={"error": {"message": "slow down"}},
            )
            response.raise_for_status()
        return transport.ModelTransportResponse(
            status_code=200, json_body=step, latency_ms=42
        )


@pytest.fixture(autouse=True)
def _no_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(effect, "_is_endpoint_healthy", lambda _: True)
    monkeypatch.setattr(effect, "_get_served_model_ids", lambda _: None)


def _run(
    monkeypatch: pytest.MonkeyPatch, script: list[Any], request: Any
) -> tuple[Any, Publisher]:
    monkeypatch.setattr(transport, "post_chat_completion", Provider(script).post)
    publisher = Publisher()
    result = effect.HandlerLlmDelegationCall().__call__(
        request, event_publisher=publisher
    )
    return result, publisher


def test_a_review_call_records_one_event_with_usage_and_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(review=_provenance())
    result, publisher = _run(monkeypatch, [_body()], request)
    assert result.success
    (event,) = publisher.cost_events()
    assert event.call_id == request.request_id
    assert event.attempt == 1
    assert event.event_id == review_call_cost_event_id(request.request_id, 1)
    assert event.provenance == _provenance()
    assert event.requested_model == MODEL
    assert event.host == "review-gateway.example.test"
    assert (event.tokens_in, event.tokens_out) == (1200, 300)
    assert event.cache_read_tokens == 900
    assert (event.cache_write_5m_tokens, event.cache_write_1h_tokens) == (100, 50)
    assert event.outcome is EnumReviewCallOutcome.OK
    assert event.stop_reason == "stop"
    assert event.latency_ms == 42
    assert event.cost_usd == result.actual_cost_usd
    assert event.cost_usd > Decimal("0")
    assert event.pricing_manifest_version == "pm-1"


def test_a_provider_that_reports_no_cache_figure_records_none_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _body(usage={"prompt_tokens": 10, "completion_tokens": 5})
    _, publisher = _run(monkeypatch, [body], _request(review=_provenance()))
    (event,) = publisher.cost_events()
    assert event.cache_read_tokens is None
    assert event.cache_write_5m_tokens is None
    assert event.cache_write_1h_tokens is None


def test_an_openai_shaped_cached_token_count_is_not_read_as_a_cache_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its prompt_tokens includes the cached part, so it is not the native meaning."""
    body = _body(
        usage={
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 64},
        }
    )
    _, publisher = _run(monkeypatch, [body], _request(review=_provenance()))
    (event,) = publisher.cost_events()
    assert event.cache_read_tokens is None


def test_a_length_stop_is_recorded_as_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, publisher = _run(
        monkeypatch, [_body(finish="length")], _request(review=_provenance())
    )
    (event,) = publisher.cost_events()
    assert event.outcome is EnumReviewCallOutcome.TRUNCATED
    assert event.stop_reason == "length"


def test_a_failed_attempt_is_recorded_as_an_upstream_error_with_no_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, publisher = _run(monkeypatch, [503], _request(review=_provenance()))
    assert not result.success
    (event,) = publisher.cost_events()
    assert event.outcome is EnumReviewCallOutcome.UPSTREAM_ERROR
    assert (event.tokens_in, event.tokens_out) == (0, 0)
    assert event.cost_usd == Decimal("0")


def test_a_call_that_is_not_a_review_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, publisher = _run(monkeypatch, [_body()], _request(review=None))
    assert publisher.cost_events() == []


def test_a_publisher_that_fails_never_fails_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Broken:
        def publish(self, topic: str, payload: Any) -> None:
            raise RuntimeError("bus down")

    monkeypatch.setattr(transport, "post_chat_completion", Provider([_body()]).post)
    result = effect.HandlerLlmDelegationCall().__call__(
        _request(review=_provenance()), event_publisher=Broken()
    )
    assert result.success


def test_without_a_publisher_the_event_goes_through_the_emit_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.events import emit_effect_topic_publisher as emit

    sent: list[dict[str, Any]] = []

    def publish(self: Any, **kwargs: Any) -> bool:
        sent.append(kwargs)
        return True

    monkeypatch.setattr(emit.EmitEffectTopicPublisher, "publish", publish)
    monkeypatch.setattr(transport, "post_chat_completion", Provider([_body()]).post)
    effect.HandlerLlmDelegationCall().__call__(_request(review=_provenance()))
    (call,) = sent
    assert call["topic"] == effect.TOPIC_REVIEW_CALL_COST_RECORDED
    assert call["payload"]["attempt"] == 1


def test_the_observation_carries_the_usage_the_record_is_built_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[ModelLlmDelegationCallObservation] = []
    token = effect.current_call_observer.set(seen.append)
    try:
        _run(monkeypatch, [_body()], _request(review=_provenance()))
    finally:
        effect.current_call_observer.reset(token)
    started, finished = seen
    assert (started.phase, started.attempt) == ("started", 1)
    assert (finished.phase, finished.attempt) == ("finished", 1)
    assert (finished.tokens_in, finished.tokens_out) == (1200, 300)
    assert finished.cache_read_tokens == 900
    assert finished.finish_reason == "stop"
    assert finished.latency_ms == 42


def test_an_attempt_made_before_a_cancellation_is_already_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first attempt finished; the invocation is cancelled during the second."""
    request = _request(review=_provenance())
    publisher = Publisher()
    seen: list[ModelLlmDelegationCallObservation] = []
    monkeypatch.setattr(
        transport,
        "post_chat_completion",
        Provider([_body(finish="length"), asyncio.CancelledError()]).post,
    )
    handler = effect.HandlerLlmDelegationCall()
    # Two attempts inside one invocation: the same call, retried once.
    token = effect.current_call_observer.set(seen.append)
    counter = effect.attempt_counter.set(iter(range(1, 100)))
    try:
        first = handler._execute_call_once(request, ENDPOINT, publisher)
        assert first.success
        with pytest.raises(asyncio.CancelledError):
            handler._execute_call_once(request, ENDPOINT, publisher)
    finally:
        effect.attempt_counter.reset(counter)
        effect.current_call_observer.reset(token)
    # The second attempt had left and never answered: it is recorded as cancelled
    # by the effect itself, as the cancellation passes through it.
    assert seen[-1].phase == "started"
    first_event, cancelled = publisher.cost_events()
    assert first_event.outcome is EnumReviewCallOutcome.TRUNCATED
    assert (cancelled.attempt, cancelled.outcome) == (
        2,
        EnumReviewCallOutcome.CANCELLED,
    )
    assert (cancelled.tokens_in, cancelled.tokens_out) == (0, 0)
    assert cancelled.cost_usd == Decimal("0")
    assert cancelled.event_id != first_event.event_id


def test_a_caller_that_ends_the_invocation_can_record_the_attempt_it_never_saw_finish() -> (
    None
):
    request = _request(review=_provenance())
    publisher = Publisher()
    started = ModelLlmDelegationCallObservation(
        phase="started", model_id=MODEL, attempt=3
    )
    effect.record_cancelled_review_attempt(
        request, started, elapsed_ms=900, event_publisher=publisher
    )
    (cancelled,) = publisher.cost_events()
    assert (cancelled.attempt, cancelled.latency_ms) == (3, 900)
    assert cancelled.outcome is EnumReviewCallOutcome.CANCELLED


def test_a_cancellation_before_the_attempt_left_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key never resolved, so no request left and there is no attempt to record."""
    request = _request(review=_provenance())
    publisher = Publisher()

    def refuse(*_: Any, **__: Any) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr(effect, "resolve_api_key_with_source_loop_safe", refuse)
    request = request.model_copy(update={"secret_ref": "llm.anthropic.api_key"})
    with pytest.raises(asyncio.CancelledError):
        effect.HandlerLlmDelegationCall()._execute_call_once(
            request, ENDPOINT, publisher
        )
    assert publisher.cost_events() == []


def test_a_cancelled_record_for_a_call_that_is_not_a_review_records_nothing() -> None:
    publisher = Publisher()
    observation = ModelLlmDelegationCallObservation(phase="started", model_id=MODEL)
    effect.record_cancelled_review_attempt(
        _request(review=None), observation, elapsed_ms=5, event_publisher=publisher
    )
    assert publisher.cost_events() == []


def test_two_attempts_of_one_invocation_get_two_distinct_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(review=_provenance())
    publisher = Publisher()
    monkeypatch.setattr(
        transport, "post_chat_completion", Provider([429, _body()]).post
    )
    handler = effect.HandlerLlmDelegationCall()
    token = effect.attempt_counter.set(iter(range(1, 100)))
    try:
        handler._execute_call_once(request, ENDPOINT, publisher)
        handler._execute_call_once(request, ENDPOINT, publisher)
    finally:
        effect.attempt_counter.reset(token)
    first, second = publisher.cost_events()
    assert (first.attempt, second.attempt) == (1, 2)
    assert first.event_id != second.event_id
    assert first.outcome is EnumReviewCallOutcome.UPSTREAM_ERROR
    assert second.outcome is EnumReviewCallOutcome.OK


def test_the_event_id_is_stable_across_replays() -> None:
    assert review_call_cost_event_id("c", 2) == review_call_cost_event_id("c", 2)
    assert review_call_cost_event_id("c", 2) != review_call_cost_event_id("c", 3)
    assert review_call_cost_event_id("c", 2) != review_call_cost_event_id("d", 2)


def test_the_effect_contract_declares_the_cost_topic_node_llm_delegation_call_effect() -> (
    None
):
    """The topic is read from the effect's own contract, never typed in the handler."""
    assert (
        effect.TOPIC_REVIEW_CALL_COST_RECORDED
        == "onex.evt.omnimarket.review-call-cost-recorded.v1"
    )
