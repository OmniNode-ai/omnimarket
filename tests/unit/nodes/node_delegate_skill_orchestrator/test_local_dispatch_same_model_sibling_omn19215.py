# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19215 AC3: a quality rejection never hops to a sibling serving the same model.

The local dispatch port hops to an untried same-tier sibling after a
quality-gate rejection (OMN-13640). That is right when the sibling serves a
DIFFERENT model: a second model is a second opinion. It is wrong when the
sibling serves the SAME model id, because a quality verdict is a verdict on the
model's draft, not on the host that ran it. Once a lane-added backend is placed
in the tier ladder as a fallback for a rung serving the same model (omnipc2 at
.202 beside local-coder at .201, both Qwen3.8-27B), every quality rejection on
.201 would buy a second, identical draft on .202 before escalating, which only
burns time.

A transport failure is different: the host was unavailable, so a same-model
sibling on another host is exactly the fallback the placement exists for. The
second test pins that the fix does not remove it.

Fixture shape: ``cheap_cloud`` declares ``cloud-primary`` and ``cloud-mirror``,
both serving ``shared-model``, then the ``claude`` tier declares a distinct
``ceiling-model``, so the only way the rejected request reaches an answer is by
escalating, and the only way it reaches ``cloud-mirror`` is the hop under test.
"""

from __future__ import annotations

import asyncio
import functools
import textwrap
from collections.abc import Generator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.handler_judge_adequacy import (
    HandlerJudgeAdequacy,
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
from tests.fixtures.judge_inference import CannedAdequacyBridge

pytestmark = pytest.mark.unit

_ROUTING_TIERS_YAML = textwrap.dedent(
    """\
    tiers:
      - name: cheap_cloud
        cost_per_1k_tokens: 0.002
        models:
          - id: shared-model
            backend_id: cloud-primary
            max_context_tokens: 8192
            use_for: [research]
          - id: shared-model
            backend_id: cloud-mirror
            max_context_tokens: 8192
            use_for: [research]
        eval_before_accept: false
        max_retries: 0
      - name: claude
        cost_per_1k_tokens: 0.01
        models:
          - id: ceiling-model
            backend_id: cloud-ceiling
            max_context_tokens: 8192
            use_for: [research]
        eval_before_accept: false
        max_retries: 0
    """
)


def _backend(backend_id: str, model_name: str, host: str) -> str:
    return textwrap.dedent(
        f"""\
          - backend_id: {backend_id}
            provider: {backend_id}
            endpoint_url: "https://{host}.test/v1/chat/completions"
            model_name: {model_name}
            tier: frontier_api
            timeout_ms: 30000
            max_tokens: 4096
            capabilities: [research]
        """
    )


_BIFROST_YAML = (
    textwrap.dedent(
        """\
        config_version: "1.0.0"
        schema_version: "bifrost_delegation.v1"
        backends:
        """
    )
    + _backend("cloud-primary", "shared-model", "primary")
    + _backend("cloud-mirror", "shared-model", "mirror")
    + _backend("cloud-ceiling", "ceiling-model", "ceiling")
    + textwrap.dedent(
        """\
        routing_rules:
          - rule_id: "7770b87c-9dc5-508d-9ee7-d7ac15acdfeb"
            priority: 10
            task_class: research
            task_class_contract_version: "1.0.0"
            backend_policy_version: "1.0.0"
            match_operation_types: [chat_completion]
            match_capabilities: [research]
            backend_ids: [cloud-primary, cloud-mirror, cloud-ceiling]
            fallback_policy:
              action: escalate_to_next_tier
              max_retries: 1
              on_exhaust: return_error
            shadow_policy_id: "9f0bcb8c-c33e-5016-a33a-f41a54b04c2b"
        default_backends:
          - cloud-primary
        """
    )
)

_TASK_CLASS_CONTRACT_YAML = textwrap.dedent(
    """\
    task_classes:
      research:
        gateway_exposure: public
        cloud_routing_policy: allowed
        pricing_ceiling_per_1k_tokens: 1.0
        definition_of_done:
          deterministic:
            - response_non_empty
          heuristic:
            - semantic_adequacy
        escalation_policy:
          max_escalations: 2
          tier_order:
            - cheap_cloud
            - claude
    """
)


@pytest.fixture
def _fixture_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """Bind the real routing authority to the fixture YAML above (OMN-13640 shape)."""
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


class _ScriptedEffect:
    """Answer per backend: ``empty`` refuses on quality, ``down`` fails transport."""

    def __init__(self, *, empty: frozenset[str], down: frozenset[str]) -> None:
        self.empty = empty
        self.down = down
        self.calls: list[str] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request.provider)
        if request.provider in self.down:
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=False,
                failure_class=EnumDelegationFailureClass.RATE_LIMITED,
                error_message="connection refused (fixture)",
            )
        content = (
            "" if request.provider in self.empty else "### ANSWER\nA sound answer."
        )
        return ModelLlmDelegationCallResult(
            request_id=request.request_id, success=True, content=content
        )


def _dispatch(effect: _ScriptedEffect, tmp_path: Path) -> dict[str, Any]:
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
        judge=HandlerJudgeAdequacy(
            inference_bridge=CannedAdequacyBridge(adequacy_score=0.95)
        ),
    )
    return asyncio.run(
        port.dispatch(
            prompt="explain the tradeoff between the two caching strategies",
            task_type="research",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    )


@pytest.mark.usefixtures("_fixture_env")
def test_quality_rejection_does_not_hop_to_a_same_model_sibling(
    tmp_path: Path,
) -> None:
    effect = _ScriptedEffect(empty=frozenset({"cloud-primary"}), down=frozenset())
    result = _dispatch(effect, tmp_path)

    assert "cloud-mirror" not in effect.calls, (
        "a quality rejection on cloud-primary hopped to cloud-mirror, which "
        f"serves the same model id. backends called: {effect.calls}"
    )
    assert effect.calls == ["cloud-primary", "cloud-ceiling"]
    assert result["status"] == "completed"


@pytest.mark.usefixtures("_fixture_env")
def test_transport_failure_still_hops_to_a_same_model_sibling(
    tmp_path: Path,
) -> None:
    """Positive control: unavailability is what a same-model sibling is for."""
    effect = _ScriptedEffect(empty=frozenset(), down=frozenset({"cloud-primary"}))
    result = _dispatch(effect, tmp_path)

    assert effect.calls == ["cloud-primary", "cloud-mirror"]
    assert result["status"] == "completed"
