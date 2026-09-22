# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RED-first regression tests for the OMN-13640 same-tier sibling gap.

The defect.

``LocalDelegationDispatchPort`` — the bus-less path ``onex delegate`` runs —
excludes the WHOLE routing tier the moment ONE backend in it fails::

    if transport_is_failure:
        current_tier = _routing_tier_name(backend)
        excluded_tiers.add(current_tier)          # <-- the tier, not the backend
        excluded_backend_refs.add(backend.backend_id)

The quality-gate FAIL branch does the same thing a few hundred lines below.
``_resolve_next_backend`` then calls ``next_eligible_tier`` with that tier
excluded, so an UNTRIED, HEALTHY sibling backend the same tier declares for the
same task class is unreachable — the ladder walks straight off the tier and,
when no higher tier can route, terminates FAILED.

The bus orchestrator does NOT have this gap: ``handler_delegation_workflow``
calls ``_maybe_retry_sibling_backend`` (OMN-14402) before its escalate-or-
terminate decision. The same-tier fallback simply was never ported to the
bus-less local path.

MEASURED, not inferred (2026-09-15, three consecutive ``onex delegate`` runs on
a 194-word prose prompt, task class ``research``)::

    local       local-heavy-reasoning  climb / heuristic_veto   (x3, max_retries)
    cheap_cloud cloud-gemini-pro       climb / rate_limited     429 free-tier quota
    -> terminal status=failed, escalation_count=1

against the live routing authority::

    sibling_backend_available_in_tier("cheap_cloud", "research",
                                      frozenset({"cloud-gemini-pro"}))  ->  "cloud-glm"
    next_eligible_tier("cheap_cloud", {"local", "cheap_cloud"}, ...)     ->  None

i.e. a healthy flat-rate sibling was declared, eligible and untried, and the
dispatch terminated FAILED without ever calling it.

Fixture shape.

``cheap_cloud`` declares TWO backends for ``research`` — ``cloud-primary``
(which fails) and ``cloud-sibling`` (which answers) — mirroring the live
``cheap_cloud`` tier's ``cloud-gemini-pro`` + ``cloud-glm`` pair. The ``claude``
tier declares the SAME backend as ``cloud-primary``, mirroring the live shape
where ``cloud-gemini-pro`` backs both tiers, so the ladder is genuinely
exhausted above ``cheap_cloud`` and the sibling is the only remaining route.
That makes the assertion unambiguous: a run that fails here failed because the
sibling was never tried, not because some other tier absorbed it.
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
      - name: local
        cost_per_1k_tokens: 0.0
        models:
          - id: local-model
            backend_id: local-x
            max_context_tokens: 8192
            use_for: [research]
        eval_before_accept: false
        max_retries: 0
      - name: cheap_cloud
        cost_per_1k_tokens: 0.002
        models:
          - id: primary-model
            backend_id: cloud-primary
            max_context_tokens: 8192
            use_for: [research]
          - id: sibling-model
            backend_id: cloud-sibling
            max_context_tokens: 8192
            use_for: [research]
        eval_before_accept: false
        max_retries: 0
      - name: claude
        cost_per_1k_tokens: 0.01
        models:
          - id: primary-model
            backend_id: cloud-primary
            max_context_tokens: 8192
            use_for: [research]
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
        capabilities: [research]
      - backend_id: cloud-primary
        provider: gemini
        endpoint_url: "https://primary.test/v1/chat/completions"
        model_name: primary-model
        tier: frontier_api
        timeout_ms: 30000
        max_tokens: 4096
        capabilities: [research]
      - backend_id: cloud-sibling
        provider: glm
        endpoint_url: "https://sibling.test/v1/chat/completions"
        model_name: sibling-model
        tier: frontier_api
        timeout_ms: 30000
        max_tokens: 4096
        capabilities: [research]
    routing_rules:
      - rule_id: "7770b87c-9dc5-508d-9ee7-d7ac15acdfeb"
        priority: 10
        task_class: research
        task_class_contract_version: "1.0.0"
        backend_policy_version: "1.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [research]
        backend_ids: [local-x, cloud-primary, cloud-sibling]
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
            - local
            - cheap_cloud
            - claude
    """
)


@pytest.fixture
def _fixture_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """Bind the routing/bifrost/task-class config to the fixture YAML above.

    Mirrors ``test_local_dispatch_tier_skip_omn15803.py``'s fixture: the defect
    lives in the SEAM between the port's dispatch loop and the real routing
    authority, so the authority is exercised for real and only its config
    source is redirected.
    """
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
    """Fail the named backends; answer from every other backend.

    A backend is named by its ``backend_id`` — that is what
    ``ModelLlmDelegationCallRequest.provider`` carries on this path.
    ``fail_class`` is the RETRYABLE class the live Gemini 429 carries
    (``RATE_LIMITED``), so the transport-failure branch under test is the exact
    one the reproduction walked.
    """

    def __init__(
        self,
        *,
        fail_providers: frozenset[str],
        fail_class: EnumDelegationFailureClass = EnumDelegationFailureClass.RATE_LIMITED,
        answer: str = "A methodical answer with enough substance to pass.",
    ) -> None:
        self.fail_providers = fail_providers
        self.fail_class = fail_class
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append((request.provider, request.model_id))
        if request.provider in self.fail_providers:
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=False,
                failure_class=self.fail_class,
                error_message="429 Too Many Requests (fixture)",
            )
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=f"### ANSWER\n{self.answer}" if self.answer else self.answer,
        )


def _dispatch(port: LocalDelegationDispatchPort) -> dict[str, Any]:
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


def _port(effect: _ScriptedEffect, tmp_path: Path) -> LocalDelegationDispatchPort:
    """Build the port with a canned judge that finds any non-empty answer adequate.

    ``research`` is not a verifiable task class, so the judge is the only
    adequacy authority it has — without one the gate refuses every draft with
    ``TASK_MISMATCH: no deterministic acceptance or judge adequacy authority``
    and the run terminates FAILED for a reason that has nothing to do with the
    routing defect under test. The judge never runs for a backend whose
    transport failed, and never lifts an answer the deterministic band already
    refused (an empty body still fails ``response_non_empty``), so it cannot
    mask either failure this file asserts on.
    """
    return LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
        judge=HandlerJudgeAdequacy(
            inference_bridge=CannedAdequacyBridge(adequacy_score=0.95)
        ),
    )


@pytest.mark.usefixtures("_fixture_env")
def test_transport_failure_tries_the_untried_sibling_in_the_same_tier(
    tmp_path: Path,
) -> None:
    """A rate-limited backend must not take its healthy tier siblings down with it.

    RED at current head: ``local-x`` and ``cloud-primary`` both fail, the port
    adds ``cheap_cloud`` to ``excluded_tiers``, ``next_eligible_tier`` finds
    nothing above it that offers a non-excluded backend, and the dispatch
    returns ``status="failed"`` having never called ``cloud-sibling``.

    GREEN after the fix: ``cloud-sibling`` is attempted and answers.
    """
    effect = _ScriptedEffect(
        fail_providers=frozenset(
            {
                "local-x",
                "cloud-primary",
            }
        )
    )
    result = _dispatch(_port(effect, tmp_path))

    called_backends = [provider for provider, _ in effect.calls]
    assert "cloud-sibling" in called_backends, (
        "cloud-sibling was never called. The tier declares it for research and "
        "it was never attempted, so the whole cheap_cloud tier was excluded on "
        f"one backend's rate limit. backends called: {called_backends}"
    )
    assert result["status"] == "completed"

    attempt_backends = [a["backend_id"] for a in result["attempts"]]
    assert attempt_backends == ["local-x", "cloud-primary", "cloud-sibling"]


@pytest.mark.usefixtures("_fixture_env")
def test_sibling_retry_is_not_charged_to_the_escalation_budget(
    tmp_path: Path,
) -> None:
    """A same-tier sibling hop is not a tier escalation and must not count as one.

    The contract's ``max_escalations`` bounds how far UP the ladder a request
    may climb. Charging a sideways hop to it would let one tier's backend count
    exhaust the budget before the request ever reaches a higher tier — the
    inverse of the bug above, and the reason the bus path's
    ``_maybe_retry_sibling_backend`` returns before ``_decide_escalation``.
    """
    effect = _ScriptedEffect(
        fail_providers=frozenset(
            {
                "local-x",
                "cloud-primary",
            }
        )
    )
    result = _dispatch(_port(effect, tmp_path))

    # local -> cheap_cloud is ONE tier escalation. cheap_cloud's primary ->
    # sibling hop is a sideways move inside that tier and adds nothing.
    assert result["escalation_count"] == 1


@pytest.mark.usefixtures("_fixture_env")
def test_quality_gate_rejection_also_tries_the_same_tier_sibling(
    tmp_path: Path,
) -> None:
    """The gate-FAIL branch carries the identical tier-exclusion defect.

    ``cloud-primary`` here RETURNS — it just returns an empty body, which the
    deterministic ``response_non_empty`` band refuses. That takes the quality-
    gate branch rather than the transport branch, and that branch adds
    ``current_tier`` to ``excluded_tiers`` in exactly the same way.
    """

    class _EmptyThenGoodEffect(_ScriptedEffect):
        def __call__(
            self, request: ModelLlmDelegationCallRequest
        ) -> ModelLlmDelegationCallResult:
            self.calls.append((request.provider, request.model_id))
            if request.provider in self.fail_providers:
                # Successful transport, refused by the deterministic band.
                return ModelLlmDelegationCallResult(
                    request_id=request.request_id,
                    success=True,
                    content="",
                )
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=True,
                content=f"### ANSWER\n{self.answer}" if self.answer else self.answer,
            )

    effect = _EmptyThenGoodEffect(
        fail_providers=frozenset(
            {
                "local-x",
                "cloud-primary",
            }
        )
    )
    result = _dispatch(_port(effect, tmp_path))

    called_backends = [provider for provider, _ in effect.calls]
    assert "cloud-sibling" in called_backends, (
        "cloud-sibling was never called after a quality-gate rejection on "
        f"cloud-primary. backends called: {called_backends}"
    )
    assert result["status"] == "completed"


@pytest.mark.usefixtures("_fixture_env")
def test_sibling_search_terminates_when_every_backend_in_the_tier_failed(
    tmp_path: Path,
) -> None:
    """Positive control for the two assertions above, and the termination bound.

    Every backend fails here, so there is no sibling left to find and the run
    MUST terminate FAILED rather than cycle inside ``cheap_cloud``. Each backend
    is attempted exactly once: the sibling search is bounded by the tier's own
    declaration list because every tried backend joins ``excluded_backend_refs``.
    """
    effect = _ScriptedEffect(
        fail_providers=frozenset(
            {
                "local-x",
                "cloud-primary",
                "cloud-sibling",
            }
        )
    )
    result = _dispatch(_port(effect, tmp_path))

    assert result["status"] == "failed"
    called = [provider for provider, _ in effect.calls]
    assert len(called) == len(set(called)), (
        f"a backend was dispatched more than once: {called}"
    )
    assert set(called) == {
        "local-x",
        "cloud-primary",
        "cloud-sibling",
    }
