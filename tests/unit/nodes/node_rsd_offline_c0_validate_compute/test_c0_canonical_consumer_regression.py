# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Offline C0 replay through the canonical delegation consumers.

The YAML and provenance below are explicitly synthetic test inputs. They are
internally aligned to exercise real consumers without claiming a live capture.
"""

from __future__ import annotations

import hashlib
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest
from omnibase_core.models.delegation.wire import ModelInferenceIntent
from omnibase_core.models.runtime.golden_chain.model_golden_chain_fixture import (
    ModelGoldenChainProvenance,
)

from omnimarket.models.delegation.wire.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    HandlerRoutingIntent,
)
from omnimarket.nodes.node_rsd_offline_c0_validate_compute.handlers.handler_rsd_offline_c0 import (
    RsdOfflineC0ValidationError,
    validate_rsd_offline_c0,
)
from omnimarket.nodes.node_rsd_offline_c0_validate_compute.models.model_rsd_offline_c0 import (
    ModelRsdOfflineC0Input,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parents[4]
_REGISTRY = (
    _ROOT / "src/omnimarket/data/model_registry/model_registry_v1.yaml"
).read_bytes()
_CORRELATION_ID = UUID("00000000-0000-0000-0000-000000000184")
_ENDPOINT = "https://synthetic-c0-consumer.invalid/v1/chat/completions"

_ROUTING_TIERS = b"""\
tiers:
  - name: local
    cost_per_1k_tokens: 0.0
    models:
      - id: Qwen3.6-35B-A3B
        backend_id: local-coder
        max_context_tokens: 65536
        use_for: [research]
    eval_before_accept: false
    max_retries: 0
"""

_BIFROST = f"""\
config_version: "1.0.0"
schema_version: "bifrost_delegation.v1"
backends:
  - backend_id: local-coder
    endpoint_url: "{_ENDPOINT}"
    model_name: Qwen3.6-35B-A3B
    tier: local
    timeout_ms: 30000
    max_tokens: 4096
    capabilities: [research]
routing_rules:
  - rule_id: "11111111-1111-4111-8111-111111111111"
    priority: 10
    task_class: research
    task_class_contract_version: "1.0.0"
    backend_policy_version: "1.0.0"
    match_operation_types: [chat_completion]
    match_capabilities: [research]
    backend_ids: [local-coder]
    fallback_policy:
      action: escalate_to_next_tier
      max_retries: 1
      on_exhaust: return_error
    shadow_policy_id: "22222222-2222-4222-8222-222222222222"
default_backends: [local-coder]
circuit_breaker:
  failure_threshold: 5
  window_seconds: 30
failover:
  max_attempts: 3
  backoff_base_ms: 500
shadow_mode:
  enabled: false
  policy_version: synthetic-test
  log_sample_rate: 1.0
  comparison_logging_enabled: true
  max_shadow_latency_ms: 5.0
""".encode("ascii")

_TASK_CLASS_CONTRACT = b"""\
task_classes:
  research:
    gateway_exposure: public
    cloud_routing_policy: allowed
    pricing_ceiling_per_1k_tokens: 1.0
    definition_of_done:
      deterministic: [response_non_empty]
      heuristic: []
    escalation_policy:
      max_escalations: 1
      tier_order: [local]
"""


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@pytest.fixture
def _synthetic_routing_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """Bind real routing consumers to aligned synthetic caller inputs."""

    tiers_path = tmp_path / "routing_tiers.yaml"
    tiers_path.write_bytes(_ROUTING_TIERS)
    bifrost_path = tmp_path / "bifrost_delegation.yaml"
    bifrost_path.write_bytes(_BIFROST)
    task_contract_path = tmp_path / "task_class_contracts.v1.yaml"
    task_contract_path.write_bytes(_TASK_CLASS_CONTRACT)
    monkeypatch.setenv("DELEGATION_ROUTING_TIERS_PATH", str(tiers_path))
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(bifrost_path))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    monkeypatch.setenv("TASK_CLASS_CONTRACT_PATH", str(task_contract_path))
    routing._config = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()
    yield
    routing._config = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()


def _request() -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Explain the synthetic route evidence.",
        task_type="research",
        correlation_id=_CORRELATION_ID,
        max_tokens=512,
        emitted_at=datetime(2026, 9, 10, tzinfo=UTC),
    )


def _c0_input(decision: ModelRoutingDecision) -> ModelRsdOfflineC0Input:
    return ModelRsdOfflineC0Input(
        routing_decision=decision,
        golden_provenance=ModelGoldenChainProvenance(
            provider="local",
            model_id="Qwen3.6-35B-A3B",
            endpoint_ref="local-coder",
            endpoint=_ENDPOINT,
            request_hash="synthetic-request-not-a-capture",
            prompt_hash="synthetic-prompt-not-a-capture",
            routing_contract_hash=_sha256(_BIFROST),
            routing_overlay_hash="none",
            recorded_at="2026-09-10T00:00:00Z",
            fixture_version="synthetic_c0_consumer_regression.v1",
        ),
        routing_tiers_yaml=_ROUTING_TIERS,
        bifrost_contract_yaml=_BIFROST,
        bifrost_overlay_yaml=None,
        model_registry_yaml=_REGISTRY,
        model_registry_key="qwen3-coder-30b",
    )


def _real_decision(workflow: HandlerDelegationWorkflow) -> ModelRoutingDecision:
    intents = workflow.handle_delegation_request(_request())
    assert len(intents) == 1
    return HandlerRoutingIntent().handle(intents[0])


def _validate_then_continue(
    workflow: HandlerDelegationWorkflow, recorded: ModelRsdOfflineC0Input
) -> tuple[object, list[ModelInferenceIntent]]:
    """Keep C0 failure ahead of the canonical workflow continuation."""

    c0 = validate_rsd_offline_c0(recorded)
    return c0, workflow.handle_routing_decision(recorded.routing_decision)


@pytest.mark.usefixtures("_synthetic_routing_environment")
def test_c0_replay_of_real_routing_consumer_reaches_real_inference_intent() -> None:
    workflow = HandlerDelegationWorkflow(workflows={})
    decision = _real_decision(workflow)

    c0, inference_intents = _validate_then_continue(workflow, _c0_input(decision))

    assert c0.non_authorizing is True
    assert c0.effects_allowed is False
    assert len(inference_intents) == 1
    inference_intent = inference_intents[0]
    assert isinstance(inference_intent, ModelInferenceIntent)
    assert inference_intent.correlation_id == _CORRELATION_ID
    assert inference_intent.base_url == decision.endpoint_url
    assert inference_intent.model == c0.served_model


@pytest.mark.usefixtures("_synthetic_routing_environment")
@pytest.mark.parametrize(
    "update",
    [
        {"selected_backend_ref": "other-backend"},
        {"selected_model": "other-model"},
        {"endpoint_url": "https://tampered.invalid/v1/chat/completions"},
    ],
)
def test_c0_replay_rejects_tampered_decision_before_consumer_continuation(
    update: dict[str, str],
) -> None:
    workflow = HandlerDelegationWorkflow(workflows={})
    decision = _real_decision(workflow)
    tampered = decision.model_copy(update=update)
    continuation = Mock(wraps=workflow.handle_routing_decision)
    workflow.handle_routing_decision = continuation  # type: ignore[method-assign]

    with pytest.raises(RsdOfflineC0ValidationError):
        _validate_then_continue(workflow, _c0_input(tampered))

    continuation.assert_not_called()


@pytest.mark.usefixtures("_synthetic_routing_environment")
def test_c0_replay_rejects_tampered_provenance_before_consumer_continuation() -> None:
    workflow = HandlerDelegationWorkflow(workflows={})
    decision = _real_decision(workflow)
    recorded = _c0_input(decision)
    tampered = recorded.model_copy(
        update={
            "golden_provenance": recorded.golden_provenance.model_copy(
                update={"routing_contract_hash": "sha256:" + "0" * 64}
            )
        }
    )
    continuation = Mock(wraps=workflow.handle_routing_decision)
    workflow.handle_routing_decision = continuation  # type: ignore[method-assign]

    with pytest.raises(RsdOfflineC0ValidationError):
        _validate_then_continue(workflow, tampered)

    continuation.assert_not_called()
