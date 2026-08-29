# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16932: the two legs that actually spend the metered quota.

Leg 1 — the JUDGE. ``HandlerQualityGateIntent.handle_async`` fires an LLM-judge
adequacy call for every ``code_generation``/``test``/``validator_generation``/
``refactor`` delegation, and that call rides ``cloud-glm-judge``, which OMN-14625
repointed onto Gemini because z.ai GLM is unreachable from ``.201``. Against a
free-tier cap of 20 requests that is ~10 delegations before the lane is dead,
and it burns quota even on delegations that would never have escalated. Once the
quota IS dead the judge fails, ``judge_score`` is ``None``, the deterministic
score stands alone, and the gate escalates — into the same dead provider. The
judge must not call a provider already known to be capped.

Leg 2 — the ESCALATION TARGET. Routing eligibility checked endpoint + secret but
never provider health, so a quota-dead backend stayed a first-class escalation
candidate forever. Within ONE workflow OMN-15503's
``transport_failed_backend_refs`` already excludes a backend after it fails, but
every NEW delegation starts with an empty exclusion set and re-burns the same
429. Quota state has to outlive the workflow.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.inference.provider_quota_policy import classify_quota_response
from omnimarket.inference.provider_quota_state import (
    clear_provider_quota_state,
    record_quota_verdict,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)

pytestmark = pytest.mark.unit

_GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
)
_GEMINI_429_BODY: dict[str, object] = {
    "error": {
        "code": 429,
        "message": (
            "You exceeded your current quota. \n* Quota exceeded for metric: "
            "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
            "limit: 20, model: gemini-2.5-flash\nPlease retry in 27.024259969s."
        ),
        "status": "RESOURCE_EXHAUSTED",
    }
}


def _disable_gemini_now(now: datetime) -> None:
    verdict = classify_quota_response(
        status_code=429,
        endpoint_url=_GEMINI_ENDPOINT,
        body=_GEMINI_429_BODY,
        now=now,
    )
    assert verdict is not None
    assert not verdict.retryable
    record_quota_verdict(endpoint_url=_GEMINI_ENDPOINT, verdict=verdict)


@pytest.fixture(autouse=True)
def _clean_quota_state() -> object:
    clear_provider_quota_state()
    yield
    clear_provider_quota_state()


# A self-contained contract whose metered rung is a REAL Gemini host, so the
# quota policy's ``match_endpoint_host`` actually matches it. The packaged
# ``routing_tiers.yaml`` puts ``cloud-gemini-pro`` in BOTH ``cheap_cloud`` and the
# ``claude`` ceiling for ``code_generation`` (OMN-14625 repointed both off the
# dead z.ai ``cloud-glm``), which is exactly the shape this ticket needs: one
# quota domain standing behind two ladder rungs. ``openrouter-qwen3-coder-480b``
# is deliberately ABSENT so ``cheap_frontier`` cannot route either — that mirrors
# the dev lane, where ``OPEN_ROUTER_API_KEY`` is not in the secret store, and it
# is what makes "every remaining rung is capped" reachable in a unit test.
_BIFROST_GEMINI_LADDER = textwrap.dedent(
    """\
    config_version: "2.0.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-coder
        endpoint_url: "http://local.test:8000/v1/chat/completions"
        model_name: qwen-coder
        tier: local
        timeout_ms: 30000
        max_tokens: 8192
        capabilities: [code_generation]
      - backend_id: cloud-gemini-pro
        endpoint_url: "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        model_name: gemini-2.5-flash
        tier: frontier_api
        timeout_ms: 60000
        max_tokens: 65536
        capabilities: [code_generation]
    routing_rules:
      - rule_id: "c0ffee00-0011-4000-8000-000000000931"
        priority: 10
        task_class: code_generation
        task_class_contract_version: "1.0.0"
        backend_policy_version: "2.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [code_generation]
        backend_ids: [local-coder, cloud-gemini-pro]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "c0ffee00-0012-4000-8000-000000000931"
    default_backends:
      - local-coder
    circuit_breaker:
      failure_threshold: 5
      window_seconds: 30
    failover:
      max_attempts: 3
      backoff_base_ms: 500
    shadow_mode:
      enabled: false
      policy_version: "test"
      log_sample_rate: 1.0
      comparison_logging_enabled: true
      max_shadow_latency_ms: 5.0
    """
)


@pytest.fixture
def gemini_ladder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Point routing at a ladder whose only metered rung is a real Gemini host."""
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_BIFROST_GEMINI_LADDER)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    routing._load_bifrost_endpoints.cache_clear()
    try:
        yield
    finally:
        routing._load_bifrost_endpoints.cache_clear()


class TestJudgeSkipsAQuotaDeadProvider:
    """Leg 1: no metered call to a provider already known to be capped."""

    @pytest.mark.asyncio
    async def test_judge_does_not_call_a_quota_disabled_backend(self) -> None:
        """RED before OMN-16932: the judge called Gemini on EVERY delegation.

        The 429 was caught, logged as ``judge-adequacy LLM call failed``, turned
        into a ``JUDGE_FAILED`` verdict — and then the next delegation did it
        again. Twelve such calls were counted in an 8h window on the dev lane.
        """
        from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.handler_judge_adequacy import (
            HandlerJudgeAdequacy,
        )

        calls: list[str] = []

        class _RecordingBridge:
            async def infer(
                self,
                model_key: str,
                system_prompt: str,
                user_prompt: str,
                timeout_seconds: float,
                temperature: float | None = None,
            ) -> str:
                calls.append(model_key)
                return '{"adequacy_score": 0.9, "reasoning": "fine"}'

            def resolved_model_id(self) -> str:
                return "gemini-2.5-flash"

            def quota_disabled(self) -> bool:
                from omnimarket.inference.provider_quota_state import (
                    quota_domain_disabled,
                )

                return quota_domain_disabled(_GEMINI_ENDPOINT) is not None

        _disable_gemini_now(datetime.now(UTC))

        judge = HandlerJudgeAdequacy(inference_bridge=_RecordingBridge())
        verdict = await judge.score(
            correlation_id=uuid4(),
            task_type="test",
            prompt="write a unit test",
            candidate_output="def test_x(): assert True",
        )

        assert calls == [], "judge called a provider whose quota is known-exhausted"
        assert verdict.actual_score is None
        assert verdict.failure_kind == "JUDGE_PROVIDER_QUOTA_EXHAUSTED"

    @pytest.mark.asyncio
    async def test_judge_still_runs_when_the_provider_is_healthy(self) -> None:
        """The skip is conditional, not a disablement of the judge."""
        from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.handler_judge_adequacy import (
            HandlerJudgeAdequacy,
        )

        calls: list[str] = []

        class _HealthyBridge:
            async def infer(
                self,
                model_key: str,
                system_prompt: str,
                user_prompt: str,
                timeout_seconds: float,
                temperature: float | None = None,
            ) -> str:
                calls.append(model_key)
                return '{"adequacy_score": 0.9, "reasoning": "fine"}'

            def resolved_model_id(self) -> str:
                return "gemini-2.5-flash"

            def quota_disabled(self) -> bool:
                return False

        judge = HandlerJudgeAdequacy(inference_bridge=_HealthyBridge())
        verdict = await judge.score(
            correlation_id=uuid4(),
            task_type="test",
            prompt="write a unit test",
            candidate_output="def test_x(): assert True",
        )

        assert len(calls) == 1
        assert verdict.actual_score == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_a_live_judge_429_records_the_disable_for_the_next_delegation(
        self,
    ) -> None:
        """The first 429 must be the LAST one — self-reinforcing loop broken.

        Before this, each delegation independently rediscovered the exhausted
        quota, so the judge kept issuing calls that could not succeed.
        """
        import httpx

        from omnimarket.inference.provider_quota_state import quota_domain_disabled
        from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.adapter_routing_resolved_judge import (
            RoutingResolvedJudgeInferenceAdapter,
        )

        adapter = RoutingResolvedJudgeInferenceAdapter()
        response = httpx.Response(
            429,
            json=_GEMINI_429_BODY,
            request=httpx.Request("POST", _GEMINI_ENDPOINT),
        )
        adapter.record_quota_failure(
            endpoint_url=_GEMINI_ENDPOINT,
            error=httpx.HTTPStatusError(
                "429", request=response.request, response=response
            ),
        )

        assert quota_domain_disabled(_GEMINI_ENDPOINT) is not None


class TestEscalationCannotTargetAQuotaDeadProvider:
    """Leg 2: escalate-to-a-corpse is structurally impossible, not merely rare."""

    def test_backend_on_a_quota_dead_domain_is_not_routable(self) -> None:
        """RED before OMN-16932.

        Eligibility was endpoint-resolvable + secret-resolvable. A backend whose
        provider had returned a non-retryable 429 minutes earlier still passed
        both, so the ladder kept selecting it.
        """
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            BifrostBackendRef,
            _backend_routable,
        )

        backend = BifrostBackendRef(
            endpoint_url=_GEMINI_ENDPOINT,
            model_name="gemini-2.5-flash",
            timeout_ms=60000,
            max_tokens=8192,
            api_key_ref=None,
        )
        assert _backend_routable(backend) is True

        _disable_gemini_now(datetime.now(UTC))
        assert _backend_routable(backend) is False

    def test_the_local_rung_is_never_collateral_damage(self) -> None:
        """A cloud cap must not take down the zero-cost rung that still works."""
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            BifrostBackendRef,
            _backend_routable,
        )

        _disable_gemini_now(datetime.now(UTC))
        local = BifrostBackendRef(
            endpoint_url="http://local.test:8000/v1/chat/completions",
            model_name="qwen3.8",
            timeout_ms=60000,
            max_tokens=122880,
            api_key_ref=None,
        )
        assert _backend_routable(local) is True

    def test_routability_returns_when_the_cap_resets(self) -> None:
        """Bounded by the provider's own stated reset — not a permanent removal."""
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            BifrostBackendRef,
            _backend_routable,
        )

        now = datetime(2026, 8, 29, 12, 13, 33, tzinfo=UTC)
        _disable_gemini_now(now)
        backend = BifrostBackendRef(
            endpoint_url=_GEMINI_ENDPOINT,
            model_name="gemini-2.5-flash",
            timeout_ms=60000,
            max_tokens=8192,
            api_key_ref=None,
        )
        assert _backend_routable(backend, now=now) is False
        assert _backend_routable(backend, now=now + timedelta(seconds=60)) is True

    def test_ladder_dead_ends_on_a_declared_terminal_not_a_429_burn(
        self, gemini_ladder: None
    ) -> None:
        """When every remaining rung is capped, escalation must STOP.

        This is the ticket's defect at the seam that actually decides it —
        ``next_eligible_tier``, the single parsing path the orchestrator calls to
        pick an escalation target — not at the private predicate underneath it.

        The failure mode being closed: ``next_eligible_tier`` handed back a tier
        whose only backend was quota-dead, the orchestrator re-routed to it, the
        provider 429'd again, and the delegation ended in ``terminal_missing``
        having spent a second metered call to learn what the first one already
        proved. With quota folded into eligibility the tier is simply not
        eligible, so the orchestrator reaches its declared
        ``no_higher_tier_available`` terminal instead.

        RED before OMN-16932: eligibility was endpoint + secret, both static, so
        the post-cap assertions below returned ``cheap_cloud`` exactly as the
        pre-cap ones did and the ladder walked straight back into the 429.
        """
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            NO_HIGHER_TIER_REASON_TOKEN,
            describe_no_higher_tier_available,
            next_eligible_tier,
        )

        # Baseline: the metered rung IS the declared next step while it is live.
        # Without this the post-cap assertion could pass vacuously on a ladder
        # that never offered ``cheap_cloud`` in the first place.
        assert (
            next_eligible_tier("local", frozenset(), task_type="code_generation")
            == "cheap_cloud"
        )

        _disable_gemini_now(datetime.now(UTC))

        # Both metered rungs in this ladder resolve the SAME Gemini backend, so
        # one cap must remove both — the quota domain is the failure domain.
        assert (
            next_eligible_tier("local", frozenset(), task_type="code_generation")
            is None
        )
        assert (
            next_eligible_tier("cheap_cloud", frozenset(), task_type="code_generation")
            is None
        )

        # And the dead end is a DECLARED terminal, not an unexplained None.
        reason = describe_no_higher_tier_available(
            current_tier_name="local",
            excluded_tiers=frozenset(),
            task_type="code_generation",
        )
        assert reason.startswith(NO_HIGHER_TIER_REASON_TOKEN)


class TestTheEscalationCallItselfRecordsTheCap:
    """The delegation call effect is the SECOND write site into the ledger.

    The judge leg usually discovers an exhausted quota first, but it only runs
    for ``JUDGE_COMBINABLE_TASK_TYPES``. For every other task class the
    escalation call itself is the ONLY place a 429 can be turned into routing
    state, so this wiring has to be covered independently — without it the
    recording call can be deleted and every judge-less class silently returns to
    re-burning a metered call per delegation to relearn the same cap.
    """

    @pytest.mark.unit
    def test_a_429_on_the_delegation_call_disables_the_domain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RED before OMN-16932: OMN-16891 classified the 429 and only LOGGED it."""
        import httpx

        from omnimarket.inference.provider_quota_state import quota_domain_disabled
        from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
            handler_llm_delegation_call as call_mod,
        )
        from omnimarket.nodes.node_llm_delegation_call_effect.handlers.transport import (
            ModelTransportResponse,
        )

        request = httpx.Request("POST", _GEMINI_ENDPOINT)
        response = httpx.Response(
            429, request=request, json=_GEMINI_429_BODY, headers={}
        )

        def fake_post(**_kwargs: object) -> ModelTransportResponse:
            raise httpx.HTTPStatusError(
                "Client error '429 Too Many Requests'",
                request=request,
                response=response,
            )

        def always_healthy(*_args: object, **_kwargs: object) -> bool:
            return True

        monkeypatch.setattr(call_mod.transport, "post_chat_completion", fake_post)
        monkeypatch.setattr(call_mod, "_is_endpoint_healthy", always_healthy)

        assert quota_domain_disabled(_GEMINI_ENDPOINT) is None

        handler = call_mod.HandlerLlmDelegationCall()
        result = handler(
            call_mod.ModelLlmDelegationCallRequest(
                request_id="req-16932",
                correlation_id="corr-16932",
                causation_id="caus-16932",
                model_id="gemini-2.5-flash",
                endpoint_ref=_GEMINI_ENDPOINT,
                prompt="write a test",
                prompt_hash="deadbeef",
                task_type="test",
                model_tier="cheap_cloud",
                provider="google-gemini",
                timeout_seconds=60.0,
            )
        )

        assert result.success is False
        # The cap is now ROUTING STATE, not just a log line — which is the whole
        # point of the ticket: the next delegation cannot select this rung.
        state = quota_domain_disabled(_GEMINI_ENDPOINT)
        assert state is not None
        assert state.quota_domain == "google-gemini"
