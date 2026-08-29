# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A 429 is not one failure class [OMN-16891].

Retrying a tier on a quota-exhaustion 429 burns latency for a GUARANTEED
failure; retrying on a billing 429 burns it forever, because no reset is
coming. The provider's own error code separates the two, so the disposition is
read from the contract-declared ``provider_quota_policy`` rather than inferred
from the status line.

Both z.ai codes below are transcribed verbatim from live probes on 2026-08-28.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.inference.provider_quota_policy import (
    EnumQuotaDisposition,
    classify_quota_response,
    load_provider_quota_policy,
)

# Verbatim live bodies, 2026-08-28.
_ZAI_1310 = {
    "error": {
        "code": "1310",
        "message": (
            "Weekly/Monthly Limit Exhausted. Your limit will reset at "
            "2026-08-30 20:32:52"
        ),
    }
}
_ZAI_1113 = {
    "error": {
        "code": "1113",
        "message": "Insufficient balance or no resource package. Please recharge.",
    }
}

_ZAI_URL = "https://api.z.ai/api/paas/v4/chat/completions"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


@pytest.fixture(scope="module")
def policy():  # type: ignore[no-untyped-def]
    return load_provider_quota_policy()


@pytest.mark.unit
class TestZaiQuotaCodes:
    def test_1310_disables_until_the_stated_reset(self, policy) -> None:  # type: ignore[no-untyped-def]
        """Code 1310 is a periodic cap — disable until the provider's reset."""
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url=_ZAI_URL,
            body=_ZAI_1310,
            policy=policy,
        )
        assert verdict.disposition is EnumQuotaDisposition.DISABLE_UNTIL_RESET
        assert verdict.provider_code == "1310"
        assert verdict.disabled_until == datetime(2026, 8, 30, 20, 32, 52, tzinfo=UTC)
        # A cap that will lift on its own is not an operator emergency.
        assert verdict.alert is False
        assert verdict.retryable is False

    def test_1113_disables_until_billing_and_alerts(self, policy) -> None:  # type: ignore[no-untyped-def]
        """Code 1113 has no reset — retrying is pure waste, so alert instead."""
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url=_ZAI_URL,
            body=_ZAI_1113,
            policy=policy,
        )
        assert verdict.disposition is EnumQuotaDisposition.DISABLE_UNTIL_BILLING
        assert verdict.provider_code == "1113"
        assert verdict.alert is True
        assert verdict.retryable is False
        # No reset instant exists to wait for; a human must act.
        assert verdict.disabled_until is None

    def test_unmapped_zai_code_falls_back_to_retryable(self, policy) -> None:  # type: ignore[no-untyped-def]
        """An unclassified 429 must not be escalated into a tier disable.

        Fail-SAFE direction: over-disabling on an unknown code would strand a
        tier on a transient throttle.
        """
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url=_ZAI_URL,
            body={"error": {"code": "9999", "message": "slow down"}},
            policy=policy,
        )
        assert verdict.disposition is EnumQuotaDisposition.RETRYABLE
        assert verdict.retryable is True

    def test_unparseable_reset_still_disables_with_a_cooldown(self, policy) -> None:  # type: ignore[no-untyped-def]
        """A cap we cannot TIME is still a cap — fail closed, not open."""
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url=_ZAI_URL,
            body={
                "error": {"code": "1310", "message": "Weekly/Monthly Limit Exhausted."}
            },
            policy=policy,
            now=datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC),
        )
        assert verdict.disposition is EnumQuotaDisposition.DISABLE_UNTIL_RESET
        assert verdict.retryable is False
        # Contract-declared fallback_cooldown_seconds: 3600.
        assert verdict.disabled_until == datetime(2026, 8, 28, 13, 0, 0, tzinfo=UTC)


@pytest.mark.unit
class TestOpenRouterAndNonQuotaResponses:
    def test_openrouter_free_tier_429_is_retryable(self, policy) -> None:  # type: ignore[no-untyped-def]
        """The free tier throttles with no structured code; it clears itself."""
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url=_OPENROUTER_URL,
            body={"error": {"message": "rate limit exceeded"}},
            policy=policy,
        )
        assert verdict.disposition is EnumQuotaDisposition.RETRYABLE
        assert verdict.retryable is True

    def test_non_429_is_not_a_quota_verdict(self, policy) -> None:  # type: ignore[no-untyped-def]
        """Only 429 carries quota semantics; 500s are ordinary failures."""
        assert (
            classify_quota_response(
                status_code=500,
                endpoint_url=_ZAI_URL,
                body={"error": {"code": "1310"}},
                policy=policy,
            )
            is None
        )

    def test_unknown_provider_host_is_retryable(self, policy) -> None:  # type: ignore[no-untyped-def]
        """A provider the contract does not describe gets the safe default."""
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url="https://example.invalid/v1/chat/completions",
            body={"error": {"code": "1310"}},
            policy=policy,
        )
        assert verdict.disposition is EnumQuotaDisposition.RETRYABLE


@pytest.mark.unit
class TestPolicyIsContractDeclared:
    def test_policy_loads_from_the_committed_contract(self, policy) -> None:  # type: ignore[no-untyped-def]
        """Tier policy is swappable by overlay, never a code literal."""
        assert policy.schema_version == "provider_quota_policy.v1"
        provider_ids = {p.provider_id for p in policy.providers}
        assert {"zai", "openrouter"} <= provider_ids

    def test_zai_codes_are_declared_not_hardcoded(self, policy) -> None:  # type: ignore[no-untyped-def]
        zai = next(p for p in policy.providers if p.provider_id == "zai")
        assert {c.code for c in zai.codes} == {"1310", "1113"}
