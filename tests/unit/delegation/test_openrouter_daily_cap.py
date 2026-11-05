# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay the daily-cap fields from the recorded 740 failed attempts.

The fixture retains the provider's classification/reset fields only. The
recorded cohort had 179 one-attempt runs and 187 three-attempt runs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from omnimarket.events.provider_quota import EnumProviderQuotaSource
from omnimarket.inference.provider_quota_observation import observe_failed_call
from omnimarket.inference.provider_quota_policy import (
    classify_quota_response,
    load_provider_quota_policy,
)
from omnimarket.inference.provider_quota_state import (
    ModelProviderQuotaSnapshot,
    quota_block_for_backend,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumQuotaDisposition,
    EnumQuotaScope,
)

pytestmark = pytest.mark.unit
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
NOW = datetime(2026, 11, 3, 14, tzinfo=UTC)
RESET = datetime(2026, 11, 4, tzinfo=UTC)
FIXTURE = Path(__file__).parents[2] / "fixtures/delegation/openrouter_daily_cap.json"


def daily_body() -> dict:
    return json.loads(FIXTURE.read_text())


def test_recorded_daily_cap_blocks_the_credential_until_its_reset() -> None:
    verdict = classify_quota_response(
        status_code=429, endpoint_url=ENDPOINT, body=daily_body(), now=NOW
    )
    assert verdict is not None
    assert verdict.disposition is EnumQuotaDisposition.DISABLE_UNTIL_RESET
    assert verdict.scope is EnumQuotaScope.PROVIDER
    assert verdict.disabled_until == RESET
    assert not verdict.retryable


@pytest.mark.parametrize("signal", ["message", "metadata"])
def test_either_explicit_daily_cap_signal_is_sufficient(signal: str) -> None:
    body = daily_body()
    if signal == "message":
        body["error"]["metadata"].pop("limit_source")
    else:
        body["error"]["message"] = "Rate limit exceeded"
    verdict = classify_quota_response(
        status_code=429, endpoint_url=ENDPOINT, body=body, now=NOW
    )
    assert verdict is not None
    assert verdict.scope is EnumQuotaScope.PROVIDER
    assert verdict.disabled_until == RESET


def test_response_reset_header_also_sets_the_daily_reset() -> None:
    body = daily_body()
    headers = body["error"]["metadata"].pop("headers")
    verdict = classify_quota_response(
        status_code=429, endpoint_url=ENDPOINT, body=body, headers=headers, now=NOW
    )
    assert verdict is not None
    assert verdict.disabled_until == RESET


@pytest.mark.parametrize("reset", [None, "bad", "NaN", "inf", "-1", "1", "1e999"])
def test_missing_invalid_or_past_reset_has_a_bounded_future_fallback(reset) -> None:
    body = daily_body()
    body["error"]["metadata"]["headers"] = {"X-RateLimit-Reset": reset}
    verdict = classify_quota_response(
        status_code=429, endpoint_url=ENDPOINT, body=body, now=NOW
    )
    assert verdict is not None
    assert verdict.scope is EnumQuotaScope.PROVIDER
    assert verdict.disabled_until == NOW + timedelta(days=1)


def test_minute_throttle_retains_the_existing_model_cooldown() -> None:
    verdict = classify_quota_response(
        status_code=429,
        endpoint_url=ENDPOINT,
        body={"error": {"code": 429, "message": "free-models-per-min"}},
        now=NOW,
    )
    assert verdict is not None
    assert verdict.disposition is EnumQuotaDisposition.COOLDOWN
    assert verdict.scope is EnumQuotaScope.MODEL
    assert verdict.disabled_until == NOW + timedelta(seconds=60)


def test_recorded_error_flows_through_observation_and_lifts_at_reset() -> None:
    error = (
        f"Client error '429 Too Many Requests' for url '{ENDPOINT}'"
        f" | provider response: {json.dumps(daily_body())}"
    )
    observation, _ = observe_failed_call(
        tenant_id=None,
        endpoint_url=ENDPOINT,
        api_key_ref="llm.openrouter.api_key",
        model_name="cohere/north-mini-code:free",
        error_message=error,
        observed_at=NOW,
        latency_ms=1,
        source=EnumProviderQuotaSource.INPROCESS_EFFECT,
    )
    assert observation is not None
    state = ModelProviderQuotaSnapshot.empty(as_of=NOW).with_observation(observation)
    for model in (
        "cohere/north-mini-code:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
    ):
        for instant, blocked in (
            (RESET - timedelta(microseconds=1), True),
            (RESET, False),
        ):
            block = quota_block_for_backend(
                state.model_copy(update={"as_of": instant}),
                endpoint_url=ENDPOINT,
                api_key_ref="llm.openrouter.api_key",
                model_name=model,
            )
            assert (block is not None) is blocked
    assert (
        quota_block_for_backend(
            state,
            endpoint_url=ENDPOINT,
            api_key_ref="other-credential",
            model_name="cohere/north-mini-code:free",
        )
        is None
    )


def test_daily_rules_keep_the_legacy_consumer_shape() -> None:
    """Reproduce CI's rejected extra keys using the unchanged base rule schema."""
    fixture = json.loads(FIXTURE.with_name("quota_rule_legacy_schema.json").read_text())
    validator = Draft202012Validator(fixture["schema"])
    policy = load_provider_quota_policy()
    rules = next(p.codes for p in policy.providers if p.provider_id == "openrouter")
    for rule in rules:
        validator.validate(rule.model_dump(mode="json"))


@pytest.mark.parametrize(
    "message",
    ["free-models-per-day", "FREE-MODELS-PER-DAY", "free-models-per-day-high-balance"],
)
def test_native_daily_marker_variants_keep_provider_scope(message: str) -> None:
    body = daily_body()
    body["error"]["message"] = f"Rate limit exceeded: {message}."
    body["error"]["metadata"].pop("limit_source")
    verdict = classify_quota_response(
        status_code=429, endpoint_url=ENDPOINT, body=body, now=NOW
    )
    assert verdict is not None
    assert verdict.scope is EnumQuotaScope.PROVIDER
    assert verdict.disabled_until == RESET


def test_malformed_limit_source_does_not_turn_a_minute_throttle_into_a_cap() -> None:
    verdict = classify_quota_response(
        status_code=429,
        endpoint_url=ENDPOINT,
        body={
            "error": {
                "code": 429,
                "message": "free-models-per-min",
                "metadata": {"limit_source": {"bad": "shape"}},
            }
        },
        now=NOW,
    )
    assert verdict is not None
    assert verdict.scope is EnumQuotaScope.MODEL
    assert verdict.disabled_until == NOW + timedelta(seconds=60)
