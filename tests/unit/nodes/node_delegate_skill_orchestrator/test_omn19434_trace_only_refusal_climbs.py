# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19434 AC3: a trace-only refusal climbs the ladder and is recorded on the attempt.

A response that is all reasoning and no answer is exactly the case a costlier
rung routinely does answer, so the refusal must CLIMB rather than terminalise,
and the attempt it refused must say WHY in the terms of the rule that fired,
not "empty response".

The ladder here is the one the OMN-19016 test builds: a free local tier with a
re-draft budget and a metered cloud tier above it. The local rung returns only
its reasoning; the cloud rung returns an answer.
"""

from __future__ import annotations

import asyncio
import functools
import json
import textwrap
from collections.abc import Generator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.delegation.reasoning_preamble import UNRESOLVED_PREAMBLE_CHECK_NAME
from omnimarket.inference.task_class_authority import (
    resolve_task_class_execution_budget,
    resolve_task_class_output_contract,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing.delegation_backend_resolution import (
    resolve_delegation_backend as _real_resolve_delegation_backend,
)

pytestmark = pytest.mark.unit

_DOCUMENT_START_MARKER = resolve_task_class_output_contract("document").start_marker

#: The local rung reasons in a paired block and stops, with no answer at all.
TRACE_ONLY = (
    "<think>\n"
    "The user wants a two-sentence summary of the ladder. I should say that it "
    "starts on the free local tier and climbs to a metered one.\n"
    "</think>\n"
)

ANSWER_BODY = (
    "The ladder starts on the free local tier and re-drafts there first. It "
    "climbs to a metered tier only when the local answer is refused."
)
ANSWER = f"{_DOCUMENT_START_MARKER}\n{ANSWER_BODY}"

_ROUTING_TIERS_YAML = textwrap.dedent(
    """\
    tiers:
      - name: local
        cost_per_1k_tokens: 0.0
        models:
          - id: local-model
            backend_id: local-x
            max_context_tokens: 8192
            use_for: [document]
        eval_before_accept: false
        max_retries: 0
      - name: cheap_cloud
        cost_per_1k_tokens: 0.002
        models:
          - id: cloud-model
            backend_id: cloud-x
            max_context_tokens: 8192
            use_for: [document]
        eval_before_accept: false
        max_retries: 0
    """
)

_BIFROST_YAML = textwrap.dedent(
    """\
    config_version: "1.0.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-x
        provider: local
        endpoint_url: "http://198.51.100.10:9000/v1/chat/completions"
        model_name: local-model
        tier: local
        timeout_ms: 30000
        max_tokens: 4096
        capabilities: [document]
      - backend_id: cloud-x
        provider: glm
        endpoint_url: "https://cloud.test/v1/chat/completions"
        model_name: cloud-model
        tier: frontier_api
        timeout_ms: 30000
        max_tokens: 4096
        capabilities: [document]
    routing_rules:
      - rule_id: "7770b87c-9dc5-508d-9ee7-d7ac15acdfeb"
        priority: 10
        task_class: document
        task_class_contract_version: "1.0.0"
        backend_policy_version: "1.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [document]
        backend_ids: [local-x, cloud-x]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "9f0bcb8c-c33e-5016-a33a-f41a54b04c2b"
    default_backends:
      - local-x
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

_TASK_CLASS_CONTRACT_YAML = textwrap.dedent(
    """\
    task_classes:
      document:
        gateway_exposure: public
        cloud_routing_policy: allowed
        pricing_ceiling_per_1k_tokens: 1.0
        quality_gate:
          required_bar: 0.8
          score_source: quality_gate_graded_score
        definition_of_done:
          deterministic:
            - response_non_empty
          heuristic:
            - no_refusal
            - semantic_adequacy
        escalation_policy:
          max_escalations: 2
          tier_order:
            - local
            - cheap_cloud
    """
)


@pytest.fixture
def _fixture_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """Bind routing/bifrost/task-class config to the fixture YAML above."""
    tiers_path = tmp_path / "routing_tiers.yaml"
    tiers_path.write_text(_ROUTING_TIERS_YAML)
    bifrost_path = tmp_path / "bifrost_delegation.yaml"
    bifrost_path.write_text(_BIFROST_YAML)
    contract_path = tmp_path / "task_class_contracts.v1.yaml"
    contract_path.write_text(_TASK_CLASS_CONTRACT_YAML)

    monkeypatch.setenv("DELEGATION_ROUTING_TIERS_PATH", str(tiers_path))
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(bifrost_path))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    monkeypatch.setenv("TASK_CLASS_CONTRACT_PATH", str(contract_path))

    monkeypatch.setattr(
        port_mod,
        "resolve_delegation_backend",
        functools.partial(
            _real_resolve_delegation_backend,
            config_path=bifrost_path,
            overlay_path=tmp_path / "no-such-overlay.yaml",
        ),
    )

    routing._config = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()
    yield
    routing._config = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()


class _TraceThenAnswer:
    """The local rung returns only its reasoning; every later rung answers."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request.provider)
        content = TRACE_ONLY if request.provider == "local-x" else ANSWER
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=content,
        )


_DOCUMENT_BUDGET = resolve_task_class_execution_budget("document")


def _dispatch(port: LocalDelegationDispatchPort) -> dict[str, Any]:
    return asyncio.run(
        port.dispatch(
            prompt="Summarise the delegation ladder for the runbook.",
            task_type="document",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=_DOCUMENT_BUDGET.task_class_timeout_ceiling_seconds,
            terminal_delivery_margin_seconds=_DOCUMENT_BUDGET.terminal_delivery_margin_seconds,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    )


@pytest.mark.usefixtures("_fixture_env")
def test_a_trace_only_rung_climbs_and_its_attempt_names_the_preamble_rule(
    tmp_path: Path,
) -> None:
    """AC3: the next rung is tried, and the refused attempt carries the reason."""
    effect = _TraceThenAnswer()
    result = _dispatch(
        LocalDelegationDispatchPort(
            effect_handler=effect,
            evidence_db_path=tmp_path / "d.sqlite",
            effect_process_boundary=False,
        )
    )

    assert effect.calls[0] == "local-x"
    assert "cloud-x" in effect.calls, (
        f"a trace-only refusal did not climb: backends called {effect.calls}"
    )
    refused = result["attempts"][0]
    assert refused["acceptance_decision"] == "climb"
    recorded = " ".join(
        str(refused.get(field) or "")
        for field in ("error_message", "acceptance_detail", "reasoning_preamble_rule")
    )
    assert (
        UNRESOLVED_PREAMBLE_CHECK_NAME in recorded or "preamble_unresolved" in recorded
    ), json.dumps(refused, default=str)
    assert "empty response" not in recorded, refused
    assert "response is empty" not in recorded, refused
    assert result["status"] == "completed", result
