# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16932: a quota-exhausted provider must stop being a routable escalation target.

Live defect this pins (dev lane, correlation ``414e11bf-…`` / ``2e0e682f-…``,
2026-08-29T12:10-12:13Z):

1. ``test`` routes to the zero-cost ``local`` rung and the local inference
   SUCCEEDS (``qwen3.8``, 81 tokens, 86-428ms).
2. The quality gate's LLM-judge leg is ITSELF a metered Gemini call. It 429s, so
   ``judge_score=None`` and the deterministic score stands alone.
3. ``passed=False`` -> escalation -> ``cheap_cloud`` -> the SAME quota-dead
   Gemini project -> 429 -> ``terminal_missing``.

The self-reinforcing part is that step 2 is what exhausts the quota in the first
place: TWO metered Gemini calls ride every ``test``/``code_generation``/
``validator_generation``/``refactor`` delegation against a free-tier cap of 20,
so the lane dies after ~10 delegations and then every subsequent delegation
escalates into the corpse.

OMN-16891 already built the contract-declared classification of a 429 — it just
never became enforcement: ``classify_quota_response`` computed a
``disabled_until`` and the only thing that consumed it was a ``logger.warning``.
These tests pin the missing half.
"""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.inference.provider_quota_policy import (
    classify_quota_response,
    load_provider_quota_policy,
)
from omnimarket.inference.provider_quota_state import (
    clear_provider_quota_state,
    quota_domain_disabled,
    record_quota_verdict,
)

pytestmark = pytest.mark.unit


# The verbatim 429 body the dev lane's Gemini backend returned on
# 2026-08-29T12:13:33Z (docker logs omninode-runtime-effects, correlation
# 414e11bf-992e-4cc9-96ad-ebed209c4e41), trimmed to the fields the classifier
# reads. ``error.code`` is the integer 429 — Gemini has no provider-native
# string code the way z.ai does — and the reset is stated as a retry DELAY, not
# as an absolute instant.
_GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
)
_GEMINI_429_BODY: dict[str, object] = {
    "error": {
        "code": 429,
        "message": (
            "You exceeded your current quota, please check your plan and billing "
            "details. For more information on this error, head to: "
            "https://ai.google.dev/gemini-api/docs/rate-limits. \n* Quota exceeded "
            "for metric: generativelanguage.googleapis.com/"
            "generate_content_free_tier_requests, limit: 20, model: gemini-2.5-flash"
            "\nPlease retry in 27.024259969s."
        ),
        "status": "RESOURCE_EXHAUSTED",
    }
}


@pytest.fixture(autouse=True)
def _clean_quota_state() -> object:
    clear_provider_quota_state()
    yield
    clear_provider_quota_state()


class TestGeminiQuotaIsClassified:
    """AC: the provider that actually 429'd on the lane must be DECLARED."""

    def test_gemini_free_tier_429_is_not_treated_as_an_ordinary_throttle(self) -> None:
        """RED before OMN-16932.

        ``provider_quota_policy`` declared only ``zai`` and ``openrouter``, so a
        Gemini 429 fell through ``_provider_for`` to ``default_disposition:
        retryable``. An exhausted daily cap was therefore classified as a
        transient throttle and nothing ever stopped routing to it.
        """
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url=_GEMINI_ENDPOINT,
            body=_GEMINI_429_BODY,
        )
        assert verdict is not None
        assert verdict.provider_id == "google-gemini"
        assert not verdict.retryable

    def test_reset_is_read_from_the_providers_stated_retry_delay(self) -> None:
        """Gemini states ``Please retry in 27.024259969s``, not an absolute stamp.

        The z.ai-shaped ``message_reset_timestamp`` parser cannot read it, so a
        cap we CAN time would otherwise fall back to the blunt cooldown.
        """
        now = datetime(2026, 8, 29, 12, 13, 33, tzinfo=UTC)
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url=_GEMINI_ENDPOINT,
            body=_GEMINI_429_BODY,
            now=now,
        )
        assert verdict is not None
        assert verdict.disabled_until is not None
        delay = (verdict.disabled_until - now).total_seconds()
        assert 27.0 <= delay <= 28.0

    def test_a_gemini_429_with_no_parseable_delay_still_disables(self) -> None:
        """Fail CLOSED: a cap we cannot time is still a cap (OMN-16891 rule)."""
        now = datetime(2026, 8, 29, 12, 13, 33, tzinfo=UTC)
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url=_GEMINI_ENDPOINT,
            body={"error": {"code": 429, "message": "quota exceeded"}},
            now=now,
        )
        assert verdict is not None
        assert not verdict.retryable
        assert verdict.disabled_until is not None
        assert verdict.disabled_until > now


class TestQuotaVerdictIsEnforcedNotJustLogged:
    """AC: the verdict must be READ BACK, not written to a log line and dropped."""

    def test_recorded_verdict_disables_the_domain_until_its_reset(self) -> None:
        now = datetime(2026, 8, 29, 12, 13, 33, tzinfo=UTC)
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url=_GEMINI_ENDPOINT,
            body=_GEMINI_429_BODY,
            now=now,
        )
        assert verdict is not None
        record_quota_verdict(endpoint_url=_GEMINI_ENDPOINT, verdict=verdict)

        assert quota_domain_disabled(_GEMINI_ENDPOINT, now=now) is not None

    def test_the_disable_lifts_on_its_own_at_the_stated_reset(self) -> None:
        """A cap is a cooldown, never a permanent ban — the rung comes back."""
        now = datetime(2026, 8, 29, 12, 13, 33, tzinfo=UTC)
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url=_GEMINI_ENDPOINT,
            body=_GEMINI_429_BODY,
            now=now,
        )
        assert verdict is not None
        record_quota_verdict(endpoint_url=_GEMINI_ENDPOINT, verdict=verdict)

        later = now + timedelta(seconds=60)
        assert quota_domain_disabled(_GEMINI_ENDPOINT, now=later) is None

    def test_the_judge_and_the_escalation_backend_share_one_quota_domain(self) -> None:
        """The whole point of keying by DOMAIN rather than backend_id.

        ``cloud-glm-judge`` (the judge leg), ``cloud-gemini-pro`` (the
        ``cheap_cloud`` escalation rung for ``test``) and ``cloud-gemini-flash``
        are three distinct ``backend_id``s that all spend the SAME Gemini
        free-tier counter with the same key. A backend-id-keyed ledger would let
        the judge's 429 be re-learned once per backend — three wasted metered
        calls to discover one exhausted quota. Tier names and backend ids are
        policy slots; the quota is the failure domain (the OMN-15503 principle,
        applied across workflows rather than within one).
        """
        now = datetime(2026, 8, 29, 12, 13, 33, tzinfo=UTC)
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url=_GEMINI_ENDPOINT,
            body=_GEMINI_429_BODY,
            now=now,
        )
        assert verdict is not None
        # Recorded from the JUDGE's endpoint (cloud-glm-judge).
        record_quota_verdict(endpoint_url=_GEMINI_ENDPOINT, verdict=verdict)

        # Observed from a DIFFERENT backend_id on the same provider host.
        escalation_endpoint = (
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        )
        assert quota_domain_disabled(escalation_endpoint, now=now) is not None

    def test_an_unrelated_provider_is_untouched(self) -> None:
        """A Gemini cap must never take the local rung or OpenRouter down."""
        now = datetime(2026, 8, 29, 12, 13, 33, tzinfo=UTC)
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url=_GEMINI_ENDPOINT,
            body=_GEMINI_429_BODY,
            now=now,
        )
        assert verdict is not None
        record_quota_verdict(endpoint_url=_GEMINI_ENDPOINT, verdict=verdict)

        assert (
            quota_domain_disabled("http://local.test:8000/v1/chat/completions", now=now)
            is None
        )
        assert (
            quota_domain_disabled(
                "https://openrouter.ai/api/v1/chat/completions", now=now
            )
            is None
        )

    def test_a_retryable_429_records_nothing(self) -> None:
        """Asymmetric on purpose: an unrecognised throttle must not strand a tier."""
        now = datetime(2026, 8, 29, 12, 13, 33, tzinfo=UTC)
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url="https://openrouter.ai/api/v1/chat/completions",
            body={"error": {"code": "rate_limited", "message": "slow down"}},
            now=now,
        )
        assert verdict is not None
        assert verdict.retryable
        record_quota_verdict(
            endpoint_url="https://openrouter.ai/api/v1/chat/completions",
            verdict=verdict,
        )
        assert (
            quota_domain_disabled(
                "https://openrouter.ai/api/v1/chat/completions", now=now
            )
            is None
        )

    def test_a_billing_gap_has_no_reset_and_stays_disabled(self) -> None:
        """``disable_until_billing`` clears only when a human funds the account."""
        now = datetime(2026, 8, 29, 12, 13, 33, tzinfo=UTC)
        verdict = classify_quota_response(
            status_code=429,
            endpoint_url="https://api.z.ai/api/paas/v4/chat/completions",
            body={"error": {"code": "1113", "message": "no balance"}},
            now=now,
        )
        assert verdict is not None
        assert verdict.disabled_until is None
        record_quota_verdict(
            endpoint_url="https://api.z.ai/api/paas/v4/chat/completions",
            verdict=verdict,
        )
        far_future = now + timedelta(days=30)
        assert (
            quota_domain_disabled(
                "https://api.z.ai/api/paas/v4/chat/completions", now=far_future
            )
            is not None
        )


class TestPolicyDeclaresTheProvidersTheLaneActuallyCalls:
    """Anti-recurrence: the committed policy must cover every metered host."""

    def test_every_metered_backend_host_is_declared_in_the_quota_policy(self) -> None:
        """RED before OMN-16932: Gemini was the lane's ONLY live metered provider
        and the one host the policy did not describe.

        Reads the committed bifrost contract rather than a hardcoded list, so a
        newly added metered backend fails this test instead of silently
        inheriting ``default_disposition: retryable``.
        """
        from urllib.parse import urlparse

        import yaml

        from omnimarket.inference.provider_quota_policy import _contract_path

        raw = yaml.safe_load(_contract_path().read_text(encoding="utf-8"))
        policy = load_provider_quota_policy()
        declared = {p.match_endpoint_host.lower() for p in policy.providers}

        undeclared: set[str] = set()
        for backend in raw["backends"]:
            url = backend.get("endpoint_url")
            if not url:
                continue
            host = (urlparse(str(url)).hostname or "").lower()
            # Only remote/metered hosts need a quota policy; a LAN rung has no
            # provider quota to exhaust. Decided by ``ipaddress`` rather than by
            # matching a private-range prefix as a string, so this covers every
            # RFC1918 block and loopback form without embedding a site-specific
            # address in a test. A non-numeric host is a real DNS name and falls
            # through to the coverage assertion below.
            if not host:
                continue
            try:
                if ipaddress.ip_address(host).is_private:
                    continue
            except ValueError:
                pass
            if host == "localhost":
                continue
            if not any(host == d or host.endswith(f".{d}") for d in declared):
                undeclared.add(host)

        assert undeclared == set(), (
            "metered backend hosts with no provider_quota_policy entry — their "
            f"429s classify as ordinary throttles and never disable: {sorted(undeclared)}"
        )
