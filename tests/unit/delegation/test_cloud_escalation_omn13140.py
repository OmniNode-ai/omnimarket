# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cloud-escalation never-cut regression suite (OMN-13140 + OMN-13143).

The hackathon headline fix: local->cloud delegation escalation must actually
FIRE and reach Google Gemini. Before OMN-13140 escalation failed closed at three
predicate gates:

  GATE 2 (quality gate) — fallback_recommended was True only on a deterministic
    fail or a REFUSAL verdict; the common WEAK_OUTPUT / TASK_MISMATCH verdicts
    returned False, so the orchestrator terminated instead of escalating.
  GATE 3 (routing) — next_eligible_tier returned None for code_generation off
    local because no cheap_cloud model declared code_generation with a resolvable
    backend, and the canonical cloud target (cloud-gemini-flash) lacked the
    code_generation capability.
  GATE 1 (orchestrator) — finish_reason=length was classed non-retryable, so a
    truncated local response terminated instead of escalating to a longer-context
    cloud successor.

These tests drive the REAL dispatch path (routing reducer -> inference effect ->
quality gate reducer -> orchestrator over their contract handlers), not handler
isolation, because handler-isolation tests pass while the live chain fails
(memory feedback_real_dispatch_path_tests).

OMN-13143 (folded in): _load_bifrost_endpoints() must fail loud (raise + emit
structured evidence) on a missing/invalid bifrost contract instead of silently
returning {}.
"""

from __future__ import annotations

import logging
import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.delegation.wire import (
    ModelInferenceIntent,
    ModelQualityGateIntent,
    ModelRoutingIntent,
)
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers import (
    handler_quality_gate,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    next_eligible_tier,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    HandlerRoutingIntent,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    HandlerInferenceIntent,
)

# Bifrost contract where local AND cheap_cloud carry resolvable code_generation
# endpoints (no api_key_ref -> usable in unit context purely on a non-empty
# endpoint_url, exactly the eligibility delta() applies). The canonical cloud
# target cloud-gemini-flash declares code_generation here, mirroring the repo
# config change. claude / cli tiers have empty endpoints so escalation that lands
# on cheap_cloud is unambiguous.
_BIFROST_CODE_GEN_CLOUD_ROUTABLE = textwrap.dedent(
    """\
    config_version: "2.0.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-coder
        endpoint_url: "http://local.test:8000/v1/chat/completions"
        model_name: qwen-coder
        tier: local
        timeout_ms: 30000
        capabilities: [code_generation]
      - backend_id: cloud-gemini-flash
        endpoint_url: "https://cloud.test/gemini/v1/chat/completions"
        model_name: gemini-2.5-flash-lite
        tier: cheap_cloud
        timeout_ms: 30000
        capabilities: [code_generation, summarization, simple_tasks, document]
      - backend_id: cloud-sonnet
        endpoint_url: ""
        model_name: claude-sonnet-4-6
        tier: frontier_api
        timeout_ms: 60000
        capabilities: [code_generation]
    routing_rules:
      - rule_id: "d4e5f6a7-0001-4000-8000-000000000001"
        priority: 10
        task_class: code_generation
        task_class_contract_version: "1.0.0"
        backend_policy_version: "2.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [code_generation]
        backend_ids: [local-coder, cloud-gemini-flash]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "e5f6a7b8-0001-4000-8000-000000000001"
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
      policy_version: "unknown"
      log_sample_rate: 1.0
      comparison_logging_enabled: true
      max_shadow_latency_ms: 5.0
    """
)

# A task-class contract that selects cloud-gemini-flash for code_generation so the
# escalation lands on the canonical Gemini target (overriding the repo default
# task_model_overrides which prefers openrouter-glm-flash, absent in this fixture).
_TASK_CLASS_CONTRACT_GEMINI = textwrap.dedent(
    """\
    schema_version: "task_class_contracts.v1"
    default_task_model_ref: gemini-flash
    task_model_overrides:
      code_generation: gemini-flash
    task_classes:
      code_generation:
        cloud_routing_policy: allowed
        pricing_ceiling_per_1k_tokens: 0.015
        escalation_policy:
          max_escalations: 2
          tier_order: [local, cheap_cloud, claude]
        definition_of_done:
          deterministic: []
          heuristic: [min_length_chars_400]
    """
)

_CANONICAL_GEMINI_BACKEND_ID = "cloud-gemini-flash"
_CANONICAL_VERTEX_BACKEND_ID = "cloud-vertex-gemini"
# OMN-13215: the ceiling tier is the HTTP cloud-sonnet backend (no shelled CLI).
_TERMINAL_CEILING_BACKEND_ID = "cloud-sonnet"


@pytest.fixture
def code_gen_cloud_routable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """Point routing at a bifrost contract + task-class contract where escalating
    a code_generation task off local lands on the canonical cloud Gemini target."""
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_BIFROST_CODE_GEN_CLOUD_ROUTABLE)
    task_class_path = tmp_path / "task_class_contracts.v1.yaml"
    task_class_path.write_text(_TASK_CLASS_CONTRACT_GEMINI)

    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    monkeypatch.setenv("TASK_CLASS_CONTRACT_PATH", str(task_class_path))

    routing._config = None
    routing._load_bifrost_endpoints.cache_clear()
    routing._get_task_class_contract.cache_clear()
    try:
        yield
    finally:
        routing._config = None
        routing._load_bifrost_endpoints.cache_clear()
        routing._get_task_class_contract.cache_clear()


def _make_request() -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Implement a function add(a, b) that returns the sum.",
        task_type="code_generation",  # type: ignore[arg-type]
        correlation_id=uuid4(),
        max_tokens=2048,
        emitted_at=datetime.now(UTC),
    )


def _httpx_response(content: str) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {
        "id": "chatcmpl-test",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
    }
    response.raise_for_status.return_value = None
    return response


# ---------------------------------------------------------------------------
# GATE 2: WEAK_OUTPUT / TASK_MISMATCH recommend fallback (boolean) AND produce an
# actual downstream escalation candidate (assert against the escalation decision).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestQualityGateVerdictRecommendsFallback:
    """GATE 2: WEAK_OUTPUT and TASK_MISMATCH verdicts set fallback_recommended."""

    def test_weak_output_contract_path_recommends_fallback(self) -> None:
        result = handler_quality_gate.delta(
            handler_quality_gate.ModelQualityGateInput(
                correlation_id=uuid4(),
                task_type="code_generation",
                llm_response_content="x = 1",
                dod_heuristic=("min_length_chars_80",),
            )
        )
        assert result.passed is False
        assert any(r.startswith("WEAK_OUTPUT") for r in result.failure_reasons)
        assert result.fallback_recommended is True

    def test_task_mismatch_contract_path_recommends_fallback(self) -> None:
        result = handler_quality_gate.delta(
            handler_quality_gate.ModelQualityGateInput(
                correlation_id=uuid4(),
                task_type="research",
                llm_response_content="The function returns a value to the caller.",
                dod_heuristic=("cites_specific_lines",),
            )
        )
        assert result.passed is False
        assert any(r.startswith("TASK_MISMATCH") for r in result.failure_reasons)
        assert result.fallback_recommended is True

    def test_weak_output_legacy_path_recommends_fallback(self) -> None:
        # Legacy path (no contract DoD): a too-short document scores 0.3 — below
        # the old quality_score<0.3 escalation gate by a hair, so it used to drop.
        result = handler_quality_gate.delta(
            handler_quality_gate.ModelQualityGateInput(
                correlation_id=uuid4(),
                task_type="document",
                llm_response_content="too short",
            )
        )
        assert result.passed is False
        assert any(r.startswith("WEAK_OUTPUT") for r in result.failure_reasons)
        assert result.fallback_recommended is True

    def test_malformed_deterministic_still_blocks_with_fallback(self) -> None:
        # Deterministic failures hard-block and recommend fallback unchanged.
        result = handler_quality_gate.delta(
            handler_quality_gate.ModelQualityGateInput(
                correlation_id=uuid4(),
                task_type="code_generation",
                llm_response_content="",
                dod_deterministic=("response_non_empty",),
            )
        )
        assert result.passed is False
        assert result.fallback_recommended is True

    def test_passing_output_does_not_recommend_fallback(self) -> None:
        result = handler_quality_gate.delta(
            handler_quality_gate.ModelQualityGateInput(
                correlation_id=uuid4(),
                task_type="code_generation",
                llm_response_content="def add(a, b):\n    return a + b\n",
                dod_heuristic=("min_length_chars_5",),
            )
        )
        assert result.passed is True
        assert result.fallback_recommended is False


@pytest.mark.unit
@pytest.mark.parametrize(
    (
        "task_type",
        "content",
        "dod_heuristic",
        "expected_prefix",
        "start_tier",
        "expected_min_tier",
    ),
    [
        # OMN-13140 closed-set tier_order: code_generation declares
        # [cheap_cloud, local, claude]. A gate failure on cheap_cloud escalates
        # forward to the next declared tier (local), which is routable in the
        # fixture. (Previously this case started on local and relied on the
        # append-unlisted bug to reach cheap_frontier — a tier code_generation's
        # tier_order does not list, so it is excluded under closed-set semantics.)
        (
            "code_generation",
            "x = 1",
            ("min_length_chars_400",),
            "WEAK_OUTPUT",
            "cheap_cloud",
            "local",
        ),
        # research declares [local, cheap_cloud, claude]: a gate failure on local
        # escalates forward to cheap_cloud (routable in the fixture).
        (
            "research",
            "The function returns a value to the caller without line refs.",
            ("cites_specific_lines",),
            "TASK_MISMATCH",
            "local",
            "cheap_cloud",
        ),
    ],
)
class TestWeakAndMismatchProduceEscalationCandidate:
    """GATE 2 (end-to-end): a WEAK_OUTPUT / TASK_MISMATCH gate result drives the
    orchestrator to emit an escalation candidate (ModelRoutingIntent), not a
    terminal failure. Asserts against the escalation DECISION, not just the bool.
    """

    def test_gate_verdict_produces_routing_escalation_candidate(
        self,
        task_type: str,
        content: str,
        dod_heuristic: tuple[str, ...],
        expected_prefix: str,
        start_tier: str,
        expected_min_tier: str,
        frontier_unconfigured_bifrost: None,
    ) -> None:
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        request = ModelDelegationRequest(
            prompt="do the thing",
            task_type=task_type,  # type: ignore[arg-type]
            correlation_id=cid,
            emitted_at=datetime.now(UTC),
        )
        handler.handle_delegation_request(request)

        from uuid import NAMESPACE_DNS, uuid5

        from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
            ModelRoutingDecision,
        )

        decision = ModelRoutingDecision(
            correlation_id=cid,
            task_type=task_type,
            selected_model="qwen-coder",
            selected_backend_id=uuid5(NAMESPACE_DNS, "omninode.ai/backends/local"),
            endpoint_url="http://local.test:8000/v1/chat/completions",
            cost_tier="low",
            max_context_tokens=65536,
            system_prompt="sp",
            rationale="r",
            tier_name=start_tier,
            dod_heuristic=dod_heuristic,
        )
        handler.handle_routing_decision(decision)
        handler.handle_inference_response(
            _inference(cid, content),
        )

        # Real quality gate produces the verdict; drive its result through the
        # orchestrator's gate-result handler exactly as the bus chain would.
        gate_result = handler_quality_gate.delta(
            handler_quality_gate.ModelQualityGateInput(
                correlation_id=cid,
                task_type=task_type,
                llm_response_content=content,
                dod_heuristic=dod_heuristic,
            )
        )
        assert any(r.startswith(expected_prefix) for r in gate_result.failure_reasons)
        assert gate_result.fallback_recommended is True

        events = handler.handle_gate_result(gate_result)

        routing_intents = [e for e in events if isinstance(e, ModelRoutingIntent)]
        assert len(routing_intents) == 1, (
            f"{expected_prefix} verdict must produce an escalation candidate"
        )
        assert routing_intents[0].min_tier_name == expected_min_tier
        assert handler.workflows[cid].state == EnumDelegationState.ROUTED
        assert handler.workflows[cid].escalation_count == 1


def _inference(cid: object, content: str) -> object:
    from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
        ModelInferenceResponseData,
    )

    return ModelInferenceResponseData(
        correlation_id=cid,  # type: ignore[arg-type]
        content=content,
        model_used="qwen-coder",
        latency_ms=10,
        prompt_tokens=10,
        completion_tokens=2,
        total_tokens=12,
    )


# ---------------------------------------------------------------------------
# GATE 3: next_eligible_tier(code_generation) == cheap_cloud, and the canonical
# Gemini target carries code_generation.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNextEligibleTierCodeGeneration:
    """GATE 3: escalation off local for code_generation must reach cheap_cloud."""

    def test_next_eligible_tier_code_generation_is_cheap_cloud(
        self, code_gen_cloud_routable: None
    ) -> None:
        result = next_eligible_tier(
            "local",
            frozenset(),
            task_type="code_generation",
        )
        assert result == "cheap_cloud"


@pytest.mark.unit
class TestCanonicalCloudTargetCapability:
    """GATE 3 (config): the canonical AI Studio Gemini target declares
    code_generation in both the bifrost capabilities and the routing tier use_for;
    Vertex stays defined but is intentionally NOT in the code_generation path.
    """

    def _bifrost(self) -> dict[str, object]:
        path = Path("src/omnimarket/configs/bifrost_delegation.yaml")
        return yaml.safe_load(path.read_text())

    def _routing_tiers(self) -> dict[str, object]:
        path = Path("src/omnimarket/configs/routing_tiers.yaml")
        return yaml.safe_load(path.read_text())

    def test_gemini_flash_bifrost_capability_includes_code_generation(self) -> None:
        by_id = {b["backend_id"]: b for b in self._bifrost()["backends"]}
        assert "code_generation" in by_id[_CANONICAL_GEMINI_BACKEND_ID]["capabilities"]

    def test_vertex_gemini_bifrost_capability_excludes_code_generation(self) -> None:
        by_id = {b["backend_id"]: b for b in self._bifrost()["backends"]}
        # Vertex stays defined (provider-agnostic) but NOT in the escalation path.
        assert _CANONICAL_VERTEX_BACKEND_ID in by_id
        assert (
            "code_generation" not in by_id[_CANONICAL_VERTEX_BACKEND_ID]["capabilities"]
        )

    def test_gemini_flash_routing_tier_use_for_includes_code_generation(self) -> None:
        tiers = {t["name"]: t for t in self._routing_tiers()["tiers"]}
        cheap_cloud = tiers["cheap_cloud"]
        gemini = next(
            m for m in cheap_cloud["models"] if m["backend_id"] == "cloud-gemini-flash"
        )
        assert "code_generation" in gemini["use_for"]

    def test_vertex_routing_tier_use_for_excludes_code_generation(self) -> None:
        tiers = {t["name"]: t for t in self._routing_tiers()["tiers"]}
        cheap_cloud = tiers["cheap_cloud"]
        vertex = next(
            m
            for m in cheap_cloud["models"]
            if m["backend_id"] == _CANONICAL_VERTEX_BACKEND_ID
        )
        assert "code_generation" not in vertex["use_for"]

    def test_canonical_gemini_endpoint_is_ai_studio(self) -> None:
        by_id = {b["backend_id"]: b for b in self._bifrost()["backends"]}
        gemini = by_id[_CANONICAL_GEMINI_BACKEND_ID]
        assert gemini["endpoint_url"] == (
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        )
        assert gemini["secret_ref"] == "llm.gemini.api_key"

    def test_terminal_claude_tier_routes_to_http_frontier_backend(self) -> None:
        """OMN-13215: the ceiling tier executes via the canonical HTTP path.

        The claude ceiling tier maps to the HTTP cloud-sonnet backend (complete
        verbatim endpoint_url + secret_ref), NOT a shelled CLI. No ``cli://`` or
        ``cli-`` backend remains in the contract.
        """
        tiers = {t["name"]: t for t in self._routing_tiers()["tiers"]}
        terminal = tiers["claude"]
        assert terminal["models"][0]["backend_id"] == _TERMINAL_CEILING_BACKEND_ID
        assert terminal["models"][0]["id"] == "claude-sonnet-4-6"

        backends = self._bifrost()["backends"]
        by_id = {b["backend_id"]: b for b in backends}
        ceiling = by_id[_TERMINAL_CEILING_BACKEND_ID]
        # Complete verbatim HTTP chat-completions URL (OMN-12815 shape).
        assert ceiling["endpoint_url"].endswith("/chat/completions")
        assert ceiling["endpoint_url"].startswith("https://")
        # Secret resolved at the effect boundary via api_key_ref (not a literal).
        assert ceiling["secret_ref"] == "llm.anthropic.api_key"

        # The shelled-CLI backends were removed entirely.
        for b in backends:
            assert not str(b["backend_id"]).startswith("cli-")
            assert not str(b.get("endpoint_url") or "").lower().startswith("cli://")


# ---------------------------------------------------------------------------
# End-to-end: code_generation escalates off local and the next routing decision
# lands on the canonical cloud Gemini target (real dispatch chain).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCodeGenerationEscalatesToGeminiCloud:
    """The never-cut headline: a code_generation task that fails the gate on local
    escalates to cheap_cloud and the routing reducer resolves the canonical Gemini
    cloud endpoint for the escalated attempt — proven over the real handler chain.
    """

    def test_weak_local_output_escalates_and_resolves_gemini_cloud(
        self, code_gen_cloud_routable: None
    ) -> None:
        workflow = HandlerDelegationWorkflow(workflows={})
        routing_handler = HandlerRoutingIntent()
        inference_handler = HandlerInferenceIntent()
        gate_handler = HandlerQualityGateIntent()
        request = _make_request()
        cid = request.correlation_id

        # Hop 1-2: orchestrator -> routing reducer -> first decision (local).
        routing_intents = workflow.handle_delegation_request(request)
        assert len(routing_intents) == 1
        decision = routing_handler.handle(routing_intents[0])
        assert decision.tier_name == "local"

        # Hop 3-4: orchestrator -> inference effect (mock a too-short local output).
        inference_intents = workflow.handle_routing_decision(decision)
        assert isinstance(inference_intents[0], ModelInferenceIntent)
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = _httpx_response("x=1")
            mock_client_cls.return_value = mock_client
            response = inference_handler.handle(inference_intents[0])

        # Hop 5-6: orchestrator -> quality gate reducer (WEAK_OUTPUT, 400-char DoD).
        gate_intents = workflow.handle_inference_response(response)
        assert isinstance(gate_intents[0], ModelQualityGateIntent)
        gate_result = gate_handler.handle(gate_intents[0])
        assert gate_result.passed is False
        assert gate_result.fallback_recommended is True

        # Hop 7: orchestrator escalates -> routing intent with min_tier_name.
        escalation = workflow.handle_gate_result(gate_result)
        escalation_intents = [
            e for e in escalation if isinstance(e, ModelRoutingIntent)
        ]
        assert len(escalation_intents) == 1
        assert escalation_intents[0].min_tier_name == "cheap_cloud"
        assert workflow.workflows[cid].state == EnumDelegationState.ROUTED

        # Hop 8: routing reducer resolves the escalated tier -> canonical Gemini
        # cloud endpoint. This is the proof escalation actually reaches Google.
        escalated_decision = routing_handler.handle(escalation_intents[0])
        assert escalated_decision.tier_name == "cheap_cloud"
        assert escalated_decision.endpoint_url == (
            "https://cloud.test/gemini/v1/chat/completions"
        )
        assert escalated_decision.selected_model == "gemini-2.5-flash-lite"


# ---------------------------------------------------------------------------
# OMN-13143: fail-loud bifrost loader.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBifrostLoaderFailsLoud:
    """OMN-13143: _load_bifrost_endpoints must raise + emit structured evidence on
    a missing/invalid contract instead of silently returning {}.
    """

    def test_missing_contract_raises_not_silent_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        missing = tmp_path / "does_not_exist.yaml"
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(missing))
        monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
        routing._load_bifrost_endpoints.cache_clear()
        try:
            with pytest.raises(ProtocolConfigurationError):
                routing._load_bifrost_endpoints()
        finally:
            routing._load_bifrost_endpoints.cache_clear()

    def test_invalid_yaml_raises_and_logs_structured_evidence(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bad = tmp_path / "bifrost_delegation.yaml"
        # A YAML mapping that fails schema validation (missing required keys).
        bad.write_text("config_version: '1.0.0'\nbackends: 'not-a-list'\n")
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(bad))
        monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
        routing._load_bifrost_endpoints.cache_clear()
        try:
            with (
                caplog.at_level(logging.ERROR),
                pytest.raises(ProtocolConfigurationError),
            ):
                routing._load_bifrost_endpoints()
            assert any(
                rec.message == "bifrost_endpoint_load_failed" for rec in caplog.records
            ), "loader must emit structured evidence on failure, not only raise"
        finally:
            routing._load_bifrost_endpoints.cache_clear()

    def test_contract_with_no_usable_endpoints_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Parses cleanly but every backend declares an empty endpoint_url, so no
        # backend endpoint is resolvable — still a misconfiguration, fail loud.
        empty_endpoints = textwrap.dedent(
            """\
            config_version: "2.0.0"
            schema_version: "bifrost_delegation.v1"
            backends:
              - backend_id: local-coder
                endpoint_url: ""
                model_name: qwen-coder
                tier: local
                timeout_ms: 30000
                capabilities: [code_generation]
            routing_rules:
              - rule_id: "d4e5f6a7-0001-4000-8000-000000000001"
                priority: 10
                task_class: code_generation
                task_class_contract_version: "1.0.0"
                backend_policy_version: "2.0.0"
                match_operation_types: [chat_completion]
                match_capabilities: [code_generation]
                backend_ids: [local-coder]
                fallback_policy:
                  action: escalate_to_next_tier
                  max_retries: 1
                  on_exhaust: return_error
                shadow_policy_id: "e5f6a7b8-0001-4000-8000-000000000001"
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
              policy_version: "unknown"
              log_sample_rate: 1.0
              comparison_logging_enabled: true
              max_shadow_latency_ms: 5.0
            """
        )
        contract = tmp_path / "bifrost_delegation.yaml"
        contract.write_text(empty_endpoints)
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract))
        monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
        routing._load_bifrost_endpoints.cache_clear()
        try:
            with pytest.raises(ProtocolConfigurationError):
                routing._load_bifrost_endpoints()
        finally:
            routing._load_bifrost_endpoints.cache_clear()
