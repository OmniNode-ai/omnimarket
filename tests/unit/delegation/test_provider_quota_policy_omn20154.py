# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Capacity refusals are a quota signal, not a retry [OMN-20154].

z.ai answered code 1302 ("rate limit reached for requests") five times in one
six-minute window on the lab dev lane on 2026-09-30 and the runtime retried
into it every time, because the policy did not map 1302 and an unmapped code
falls through to ``retryable``. A retryable verdict records nothing, so the
next delegation walked into the same throttle.

These tests pin the contract-declared answer: a capacity refusal is a
``cooldown`` (skip that key until the provider's Retry-After, or the declared
fallback, and take the next rung now), scoped to the provider or to one model
as the contract says.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.inference.provider_quota_policy import (
    EnumQuotaDisposition,
    classify_quota_response,
    load_provider_quota_policy,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumQuotaScope,
)

_NOW = datetime(2026, 9, 30, 13, 45, 0, tzinfo=UTC)
_ZAI_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

# Live shape, lab dev lane 2026-09-30T13:42Z (docker logs omninode-runtime-effects).
_ZAI_1302 = {
    "error": {
        "code": "1302",
        "message": "Rate limit reached for requests",
    }
}
_OPENROUTER_429 = {
    "error": {
        "code": 429,
        "message": "Rate limit exceeded: free-models-per-min.",
    }
}
_GEMINI_EXHAUSTED = {
    "error": {
        "code": 429,
        "status": "RESOURCE_EXHAUSTED",
        "message": (
            "You exceeded your current quota. Quota exceeded for metric: "
            "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
            "limit: 20, model: gemini-2.5-flash\nPlease retry in 27.5s."
        ),
    }
}


@pytest.fixture(scope="module")
def policy():  # type: ignore[no-untyped-def]
    return load_provider_quota_policy()


@pytest.mark.unit
class TestZai1302IsACapacityCooldown:
    def test_1302_is_a_cooldown_not_a_retry(self, policy) -> None:  # type: ignore[no-untyped-def]
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url=_ZAI_URL,
            body=_ZAI_1302,
            policy=policy,
            now=_NOW,
        )
        assert verdict is not None
        assert verdict.disposition is EnumQuotaDisposition.COOLDOWN
        assert verdict.retryable is False
        assert verdict.provider_id == "zai"
        assert verdict.provider_code == "1302"
        # Model scope: the flash -> glm-5.3 fall-through answers on the other
        # model (OMN-19432), so only the model that refused is cooled down.
        assert verdict.scope is EnumQuotaScope.MODEL
        assert verdict.alert is False

    def test_1302_without_retry_after_uses_the_declared_fallback(self, policy) -> None:  # type: ignore[no-untyped-def]
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url=_ZAI_URL,
            body=_ZAI_1302,
            policy=policy,
            now=_NOW,
        )
        assert verdict is not None
        assert verdict.disabled_until == _NOW + timedelta(seconds=60)

    def test_1302_respects_the_retry_after_header(self, policy) -> None:  # type: ignore[no-untyped-def]
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url=_ZAI_URL,
            body=_ZAI_1302,
            headers={"Retry-After": "17"},
            policy=policy,
            now=_NOW,
        )
        assert verdict is not None
        assert verdict.disabled_until == _NOW + timedelta(seconds=17)

    def test_retry_after_as_an_http_date_is_read(self, policy) -> None:  # type: ignore[no-untyped-def]
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url=_ZAI_URL,
            body=_ZAI_1302,
            headers={"retry-after": "Wed, 30 Sep 2026 13:47:00 GMT"},
            policy=policy,
            now=_NOW,
        )
        assert verdict is not None
        assert verdict.disabled_until == datetime(2026, 9, 30, 13, 47, 0, tzinfo=UTC)


@pytest.mark.unit
class TestOpenRouter429IsPerModel:
    def test_openrouter_429_cools_down_one_model(self, policy) -> None:  # type: ignore[no-untyped-def]
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url=_OPENROUTER_URL,
            body=_OPENROUTER_429,
            policy=policy,
            now=_NOW,
        )
        assert verdict is not None
        assert verdict.disposition is EnumQuotaDisposition.COOLDOWN
        assert verdict.scope is EnumQuotaScope.MODEL
        assert verdict.provider_id == "openrouter"
        assert verdict.disabled_until is not None
        assert verdict.disabled_until > _NOW


@pytest.mark.unit
class TestGoogleResourceExhausted:
    def test_resource_exhausted_status_is_matched_per_model(self, policy) -> None:  # type: ignore[no-untyped-def]
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url=_GEMINI_URL,
            body=_GEMINI_EXHAUSTED,
            policy=policy,
            now=_NOW,
        )
        assert verdict is not None
        assert verdict.provider_code == "RESOURCE_EXHAUSTED"
        assert verdict.disposition is EnumQuotaDisposition.DISABLE_UNTIL_RESET
        # Gemini free-tier counters are per model ("model: gemini-2.5-flash").
        assert verdict.scope is EnumQuotaScope.MODEL
        assert verdict.disabled_until == _NOW + timedelta(seconds=27.5)


@pytest.mark.unit
def test_every_rule_declares_a_scope(policy) -> None:  # type: ignore[no-untyped-def]
    """Scope is part of the quota key; no rule may leave it to a code default."""
    for provider in policy.providers:
        for rule in provider.codes:
            assert isinstance(rule.scope, EnumQuotaScope), (provider, rule)


@pytest.mark.unit
def test_a_cooldown_rule_must_declare_a_fallback(policy) -> None:  # type: ignore[no-untyped-def]
    """A cooldown with a zero fallback would bar nothing when no Retry-After arrives."""
    for provider in policy.providers:
        for rule in provider.codes:
            if rule.disposition is EnumQuotaDisposition.COOLDOWN:
                assert rule.fallback_cooldown_seconds > 0, (provider, rule)
