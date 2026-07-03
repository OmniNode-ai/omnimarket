# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13359: node generation rides the delegation routing ladder.

Before this ticket the generation consumer resolved a single model statically
from the contract and called it on EVERY attempt. It emitted a "would escalate"
proof event on quality-gate (contract-validation) failure but kept hammering the
same local model — the ladder was advisory, never effective.

This module proves the corrected behavior end-to-end through the REAL dispatch
path (the non-injected ``_call_llm`` branch that builds a real
``ModelLlmInferenceRequest`` and resolves the endpoint from the routing
authority):

  1. Local-first: attempt #1 routes to the contract-declared starting tier
     (``local`` / ``local-coder``) — the same model/endpoint the contract
     declares, posted verbatim.
  2. Escalation on QG fail: when attempt #1's artifact fails contract
     validation, the NEXT attempt routes to a DIFFERENT (escalated) tier's
     model + endpoint, selected by the routing authority — not the starting
     model again.
  3. The quality gate decides acceptance: a contract-valid attempt stops the
     run (no further escalation).

The routing authority is exercised for real (``delta`` / ``next_eligible_tier``)
against a hermetic bifrost fixture; the wire model + POST endpoint of EACH
attempt are captured and asserted. No network, no Kafka, no Docker.
"""

from __future__ import annotations

import asyncio
import textwrap
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
    HandlerGenerationConsumer,
)
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelNodeGenerationRequest,
)

# ---------------------------------------------------------------------------
# LLM-response fixtures (a contract-valid vs contract-invalid generation).
# ---------------------------------------------------------------------------

_VALID_CONTRACT_YAML = """\
name: node_stub_compute
contract_version: "1.0.0"
node_type: compute
input_model:
  name: ModelStubInput
  module: omnimarket.nodes.node_stub_compute.models
output_model:
  name: ModelStubOutput
  module: omnimarket.nodes.node_stub_compute.models
"""

_VALID_HANDLER_SOURCE = """\
def handle(input_data):
    return {"result": input_data}
"""

_VALID_LLM_RESPONSE = (
    "```yaml\n" + _VALID_CONTRACT_YAML + "```\n\n"
    "```python\n" + _VALID_HANDLER_SOURCE + "```\n"
)

# A response whose YAML does not parse to a mapping → contract validation fails.
_INVALID_LLM_RESPONSE = (
    "```yaml\nnot_a_mapping: [broken\n```\n\n"
    "```python\n" + _VALID_HANDLER_SOURCE + "```\n"
)

# Distinct endpoints per tier so the captured POST URL proves WHICH tier ran.
_LOCAL_ENDPOINT = "http://100.109.203.94:8000/v1/chat/completions"  # onex-allow-internal-ip OMN-13359 reason="test fixture: local-coder backend endpoint"
_CLOUD_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
)
_LOCAL_MODEL = "Qwen3.6-35B-A3B"
_CLOUD_MODEL = "gemini-flash"


def _bifrost_contract() -> str:
    """A two-tier bifrost contract: local-coder (local) + cloud-gemini-flash."""
    return textwrap.dedent(
        f"""\
        config_version: "2.0.0"
        schema_version: "bifrost_delegation.v1"
        backends:
          - backend_id: local-coder
            endpoint_url: "{_LOCAL_ENDPOINT}"
            model_name: "{_LOCAL_MODEL}"
            tier: local
            timeout_ms: 60000
            max_tokens: 65536
            capabilities: [code_generation]
          - backend_id: cloud-gemini-flash
            endpoint_url: "{_CLOUD_ENDPOINT}"
            model_name: "{_CLOUD_MODEL}"
            api_key_env: GEMINI_API_KEY
            tier: cheap_cloud
            timeout_ms: 60000
            max_tokens: 8192
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
        default_backends: [local-coder, cloud-gemini-flash]
        """
    )


def _routing_tiers() -> str:
    """A minimal local→cheap_cloud routing ladder (does NOT touch the repo file)."""
    return textwrap.dedent(
        f"""\
        tiers:
          - name: local
            cost_per_1k_tokens: 0.0
            models:
              - id: {_LOCAL_MODEL}
                backend_id: local-coder
                max_context_tokens: 65536
                use_for: [code_generation]
            eval_before_accept: true
            eval_model: qwen
            max_retries: 1
          - name: cheap_cloud
            cost_per_1k_tokens: 0.002
            models:
              - id: {_CLOUD_MODEL}
                backend_id: cloud-gemini-flash
                max_context_tokens: 1000000
                use_for: [code_generation]
            eval_before_accept: true
            eval_model: qwen
            max_retries: 1
        """
    )


def _task_class_contract() -> str:
    """code_generation escalation ladder local→cheap_cloud (local FIRST).

    The ticket requires generation try local first, then escalate per the
    ladder. We declare a hermetic task-class contract (NOT the repo file) whose
    code_generation tier_order is [local, cheap_cloud] so the assertions are
    independent of the repo's operational ordering.
    """
    return textwrap.dedent(
        """\
        task_classes:
          code_generation:
            cloud_routing_policy: allowed
            escalation_policy:
              tier_order:
                - local
                - cheap_cloud
        """
    )


def _generation_contract(tmp_path: Path) -> Path:
    """Generation contract starting on the local tier (endpoint_ref=local-coder)."""
    contract = {
        "name": "node_generation_consumer",
        "contract_version": {"major": 1, "minor": 0, "patch": 0},
        "node_type": "orchestrator",
        "node_version": {"major": 1, "minor": 0, "patch": 0},
        "event_bus": {
            "publish_topics": [
                "onex.evt.omnimarket.node-generation-completed.v1",
                "onex.evt.omnimarket.node-generation-failed.v1",
                "onex.cmd.omnimarket.node-deploy.v1",
                "onex.evt.platform.node-registration.v1",
                "onex.evt.omnimarket.delegation-escalation-triggered.v1",
            ],
            "subscribe_topics": [],
        },
        "model_routing": {
            "provider": "local",
            "served_model_id": _LOCAL_MODEL,
            "endpoint_ref": "local-coder",
            "routing_source": "contract",
            "task_type": "code_generation",
        },
    }
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.dump(contract))
    return path


class _CapturedCall:
    def __init__(self, model: str, endpoint_url: str) -> None:
        self.model = model
        self.endpoint_url = endpoint_url


class _FakeUsage:
    def __init__(self) -> None:
        self.tokens_input = 10
        self.tokens_output = 20
        self.tokens_total = 30
        self.usage_source = "api"


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.generated_text = text
        self.usage = _FakeUsage()
        self.latency_ms = 100.0


class _RouteCapturingEffect:
    """Captures the (model, endpoint_url) of EACH built request.

    Critically, this is used on the NON-injected code path (``_injected_effect
    = False``) so the handler builds the real ``ModelLlmInferenceRequest`` and
    resolves the endpoint from the routing authority — the captures are the live
    routing truth, not a restatement of handler internals.
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[_CapturedCall] = []

    async def handle(self, request: Any) -> _FakeResponse:
        await asyncio.sleep(0)
        self.calls.append(_CapturedCall(request.model, request.endpoint_url))
        text = self._responses.pop(0) if self._responses else _VALID_LLM_RESPONSE
        return _FakeResponse(text)


@pytest.fixture
def hermetic_routing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Generator[None, None, None]:
    """Point every routing-authority config at hermetic fixtures + isolate state."""
    from omnimarket.inference import secret_store_resolver
    from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
        handler_delegation_routing as routing_handler,
    )

    bifrost = tmp_path / "bifrost_delegation.yaml"
    bifrost.write_text(_bifrost_contract())
    overlay = tmp_path / "__no_overlay__.yaml"
    tiers = tmp_path / "routing_tiers.yaml"
    tiers.write_text(_routing_tiers())
    task_classes = tmp_path / "task_class_contracts.v1.yaml"
    task_classes.write_text(_task_class_contract())

    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(bifrost))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay))
    monkeypatch.setenv("DELEGATION_ROUTING_TIERS_PATH", str(tiers))
    monkeypatch.setenv("TASK_CLASS_CONTRACT_PATH", str(task_classes))

    # Make the cloud backend's secret ref resolvable so the cheap_cloud tier is
    # selectable by the authority (the secret VALUE is never posted in this test).
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")

    # Isolate the replay-state dir so handle() never short-circuits on a stale
    # benchmark marker leaked from a shared ONEX_STATE_DIR.
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "onex_state"))
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)

    secret_store_resolver.clear_secret_store_resolver_cache()
    routing_handler._config = None
    routing_handler._get_task_class_contract.cache_clear()
    routing_handler._load_bifrost_endpoints.cache_clear()
    yield
    secret_store_resolver.clear_secret_store_resolver_cache()
    routing_handler._config = None
    routing_handler._get_task_class_contract.cache_clear()
    routing_handler._load_bifrost_endpoints.cache_clear()


def _make_real_path_handler(
    tmp_path: Path,
    responses: list[str],
    published: list[tuple[str, bytes]] | None = None,
) -> tuple[HandlerGenerationConsumer, _RouteCapturingEffect]:
    captures = [] if published is None else published
    effect = _RouteCapturingEffect(responses)
    handler = HandlerGenerationConsumer(
        event_publisher=lambda t, p: captures.append((t, p)),
        contract_path=_generation_contract(tmp_path),
    )
    # Force the real request-building branch while capturing the built request.
    handler._effect = effect
    handler._injected_effect = False
    return handler, effect


# ---------------------------------------------------------------------------
# Local-first: attempt #1 rides the contract starting tier.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.usefixtures("hermetic_routing")
@pytest.mark.asyncio
async def test_first_attempt_routes_to_local_tier(tmp_path: Path) -> None:
    """Attempt #1 posts to the contract-declared local model + endpoint."""
    handler, effect = _make_real_path_handler(tmp_path, [_VALID_LLM_RESPONSE])

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-ladder-local-1",
            max_attempts=3,
        )
    )

    assert result.contract_passed is True
    assert result.attempt_count == 1
    assert len(effect.calls) == 1
    # The single call rode the local tier — local model, local endpoint verbatim.
    assert effect.calls[0].model == _LOCAL_MODEL
    assert effect.calls[0].endpoint_url == _LOCAL_ENDPOINT
    # The first per-attempt record carries the local route.
    assert result.attempts[0].provider == "local"
    assert result.attempts[0].model_id == _LOCAL_MODEL


# ---------------------------------------------------------------------------
# Escalation on quality-gate failure: attempt #2 rides the NEXT tier.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.usefixtures("hermetic_routing")
@pytest.mark.asyncio
async def test_escalates_to_cloud_tier_on_validation_failure(tmp_path: Path) -> None:
    """When attempt #1 fails the quality gate, attempt #2 rides the cloud tier.

    This is the core OMN-13359 proof: the SECOND call's wire model + POST
    endpoint must be the cheap_cloud tier's — selected by the routing authority,
    not the starting local model again.
    """
    handler, effect = _make_real_path_handler(
        tmp_path,
        [_INVALID_LLM_RESPONSE, _VALID_LLM_RESPONSE],
    )

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-ladder-escalate-1",
            max_attempts=3,
        )
    )

    assert result.attempt_count == 2
    assert len(effect.calls) == 2
    # Attempt #1: local tier (the contract starting route).
    assert effect.calls[0].model == _LOCAL_MODEL
    assert effect.calls[0].endpoint_url == _LOCAL_ENDPOINT
    # Attempt #2: escalated to the cloud tier — DIFFERENT model AND endpoint.
    assert effect.calls[1].model == _CLOUD_MODEL
    assert effect.calls[1].endpoint_url == _CLOUD_ENDPOINT
    assert effect.calls[1].endpoint_url != effect.calls[0].endpoint_url
    # The second attempt rode the escalated cloud route (recorded honestly).
    assert result.attempts[1].provider == "cloud"
    assert result.attempts[1].model_id == _CLOUD_MODEL
    # The QG accepted the escalated attempt, so the run stopped (no third call).
    assert result.contract_passed is True


@pytest.mark.unit
@pytest.mark.usefixtures("hermetic_routing")
@pytest.mark.asyncio
async def test_quality_gate_stops_run_no_escalation_on_first_pass(
    tmp_path: Path,
) -> None:
    """A first-attempt quality-gate pass must NOT escalate — stays local-only."""
    handler, effect = _make_real_path_handler(
        tmp_path,
        [_VALID_LLM_RESPONSE, _VALID_LLM_RESPONSE],
    )

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-ladder-firstpass-1",
            max_attempts=3,
        )
    )

    assert result.attempt_count == 1
    assert len(effect.calls) == 1
    # Never left the local tier.
    assert all(c.model == _LOCAL_MODEL for c in effect.calls)
    assert all(c.endpoint_url == _LOCAL_ENDPOINT for c in effect.calls)


@pytest.mark.unit
@pytest.mark.usefixtures("hermetic_routing")
@pytest.mark.asyncio
async def test_benchmark_records_escalated_tier_as_final_route(
    tmp_path: Path,
) -> None:
    """The run-level benchmark identity reflects the tier that produced the artifact.

    After escalation the accepted artifact came from the cloud tier, so the
    benchmark provider/model/endpoint and resolved_endpoint must be the cloud
    route — not the contract starting tier.
    """
    handler, _ = _make_real_path_handler(
        tmp_path,
        [_INVALID_LLM_RESPONSE, _VALID_LLM_RESPONSE],
    )

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-ladder-bench-1",
            max_attempts=3,
        )
    )

    assert result.provider == "cloud"
    assert result.model_id == _CLOUD_MODEL
    assert result.resolved_endpoint == _CLOUD_ENDPOINT
    # cost_basis follows the final (cloud) provider, not local_free.
    assert result.cost_basis != "local_free"


@pytest.mark.unit
@pytest.mark.usefixtures("hermetic_routing")
@pytest.mark.asyncio
async def test_emits_escalation_proof_matching_active_route(tmp_path: Path) -> None:
    """The escalation proof event names the SAME tier the next attempt actually rode.

    Before OMN-13359 the proof and the executed model could diverge — the event
    said "cheap_cloud" while the next call still hit local. They must agree.
    """
    import json

    published: list[tuple[str, bytes]] = []
    handler, effect = _make_real_path_handler(
        tmp_path,
        [_INVALID_LLM_RESPONSE, _VALID_LLM_RESPONSE],
        published=published,
    )

    await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-ladder-proof-1",
            max_attempts=3,
        )
    )

    escalation_events = [
        json.loads(p)
        for t, p in published
        if t == "onex.evt.omnimarket.delegation-escalation-triggered.v1"
    ]
    assert len(escalation_events) == 1
    event = escalation_events[0]
    # The proof's tier/model/endpoint match the second (escalated) call.
    assert event["tier"] == "cheap_cloud"
    assert event["model"] == effect.calls[1].model == _CLOUD_MODEL
    assert event["endpoint"] == effect.calls[1].endpoint_url == _CLOUD_ENDPOINT
