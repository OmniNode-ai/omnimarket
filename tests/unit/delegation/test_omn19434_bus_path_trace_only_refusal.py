# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19434 (bus path): a trace-only response reaches the gate as reasoning, not as a blank.

On the message-bus path the orchestrator extracts the declared deliverable at
ingest. A response that is reasoning with no answer behind it carries no
marker, so extraction refuses it and blanks it, and the gate intent used to
carry that blank: the gate then reported ``MALFORMED: empty response`` about a
response that was all reasoning (the OMN-19224 shape). The gate intent now
carries the raw text for that one case, the gate's preamble floor names the
real problem, and the caller-facing content stays blank.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import (
    ModelInferenceIntent,
    ModelInferenceResponseData,
    ModelQualityGateIntent,
    ModelRoutingIntent,
)

from omnimarket.delegation.reasoning_preamble import UNRESOLVED_PREAMBLE_CHECK_NAME
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    HandlerRoutingIntent,
)

pytestmark = pytest.mark.unit

_BIFROST_SUMMARIZATION = (
    "config_version: '2.0.0'\n"
    "schema_version: bifrost_delegation.v1\n"
    "backends:\n"
    "  - backend_id: cloud-gemini-flash\n"
    "    provider: gemini\n"
    '    endpoint_url: "http://test-summarizer:8000/v1/chat/completions"\n'
    '    model_name: "gemini-2.5-flash-lite"\n'
    "    tier: cheap_cloud\n"
    "    timeout_ms: 30000\n"
    "    capabilities: [summarization, simple_tasks, document, code_generation]\n"
    "routing_rules:\n"
    '  - rule_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"\n'
    "    priority: 10\n"
    "    task_class: summarization\n"
    '    task_class_contract_version: "1.0.0"\n'
    '    backend_policy_version: "2.0.0"\n'
    "    match_operation_types: [chat_completion]\n"
    "    match_capabilities: [summarization]\n"
    "    backend_ids: [cloud-gemini-flash]\n"
    "    fallback_policy:\n"
    "      action: escalate_to_next_tier\n"
    "      max_retries: 1\n"
    "      on_exhaust: return_error\n"
    '    shadow_policy_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"\n'
    "default_backends:\n"
    "  - cloud-gemini-flash\n"
    "circuit_breaker:\n"
    "  failure_threshold: 5\n"
    "  window_seconds: 30\n"
    "failover:\n"
    "  max_attempts: 3\n"
    "  backoff_base_ms: 500\n"
    "shadow_mode:\n"
    "  enabled: false\n"
    '  policy_version: "test"\n'
    "  log_sample_rate: 1.0\n"
    "  comparison_logging_enabled: true\n"
    "  max_shadow_latency_ms: 5.0\n"
)

#: Reasoning in a paired block, and nothing after it: no marker, no answer.
_TRACE_ONLY = (
    "<think>\n"
    "The user wants a one-line summary. The summary is that the gate now "
    "names a reasoning-only response for what it is.\n"
    "</think>\n"
)

#: An ordinary answer with no marker: refused by extraction, and NOT reasoning.
_UNMARKED_ANSWER = (
    "The change binds the bus orchestrator's truncation classifier to the "
    "shared constant the inference effect raises, so the two cannot drift."
)


@pytest.fixture(autouse=True)
def _bifrost_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    import omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing as _h

    _h._config = None
    _h._load_bifrost_endpoints.cache_clear()
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_BIFROST_SUMMARIZATION, encoding="utf-8")
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    yield
    _h._config = None
    _h._load_bifrost_endpoints.cache_clear()


def _drive(
    content: str,
) -> tuple[HandlerDelegationWorkflow, ModelDelegationRequest, ModelQualityGateIntent]:
    workflow = HandlerDelegationWorkflow(workflows={})
    request = ModelDelegationRequest(
        prompt="Summarize what the reasoning-preamble boundary rule changed.",
        task_type="summarization",
        correlation_id=uuid4(),
        max_tokens=512,
        emitted_at=datetime.now(UTC),
    )
    routing_intents = workflow.handle_delegation_request(request)
    assert isinstance(routing_intents[0], ModelRoutingIntent)
    decision = HandlerRoutingIntent().handle(routing_intents[0])
    inference_intents = workflow.handle_routing_decision(decision)
    intent = inference_intents[0]
    assert isinstance(intent, ModelInferenceIntent)
    response = ModelInferenceResponseData(
        correlation_id=intent.correlation_id,
        inference_attempt_id=intent.inference_attempt_id,
        content=content,
        model_used=intent.model,
        llm_call_id="chatcmpl-test",
        latency_ms=1,
        prompt_tokens=64,
        completion_tokens=128,
        total_tokens=192,
    )
    gate_intents = workflow.handle_inference_response(response)
    assert len(gate_intents) == 1
    gate_intent = gate_intents[0]
    assert isinstance(gate_intent, ModelQualityGateIntent)
    return workflow, request, gate_intent


def test_the_gate_judges_the_reasoning_and_names_the_preamble_rule() -> None:
    """AC1 on the bus path: the verdict names the rule, not an empty response."""
    _, _, gate_intent = _drive(_TRACE_ONLY)

    assert gate_intent.payload.llm_response_content == _TRACE_ONLY
    result = HandlerQualityGateIntent().handle(gate_intent)
    assert not result.passed
    failed = {e.rule for e in result.rule_evaluations if not e.passed}
    assert failed == {UNRESOLVED_PREAMBLE_CHECK_NAME}, failed
    for reason in result.failure_reasons:
        assert "empty response" not in reason, reason
        assert "response is empty" not in reason, reason
    # The refusal climbs: the next rung brings a fresh attempt.
    assert result.fallback_recommended


def test_the_caller_still_receives_nothing_of_the_reasoning() -> None:
    """The raw text reaches the gate only; the recorded content stays blank."""
    workflow, request, _ = _drive(_TRACE_ONLY)
    state = workflow._workflows[request.correlation_id]
    assert state.inference_content == ""
    assert state.output_refusal is not None


def test_an_unmarked_answer_is_unchanged() -> None:
    """Negative control: an ordinary unmarked answer is not reasoning, so the
    gate still receives the blank the extraction refusal produced."""
    workflow, request, gate_intent = _drive(_UNMARKED_ANSWER)
    state = workflow._workflows[request.correlation_id]
    assert gate_intent.payload.llm_response_content == ""
    assert state.inference_content == ""
    assert state.output_refusal is not None
    assert state.gate_content_override is None
