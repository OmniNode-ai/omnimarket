# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21214 AC1: every rung's deadline keeps time in reserve for the rungs after it.

A class has one execution budget for the whole chain. Before this change each
attempt ran on its backend's own timeout, so three slow rungs could spend the
whole budget and leave the last rung nothing. Now an attempt's deadline is the
smaller of its backend's timeout and the chain time left minus
``rung_floor_seconds`` for every rung that can still follow.

The ladder below has four rungs. The first holds three backends, so it is tried
three times (two retries on siblings in the same rung). Every attempt on the
first three rungs runs until the dispatch kills it: the fake effect advances an
injected clock past the deadline it was given. The fourth rung answers. With
the reserve, it starts with at least one floor of the budget left and the run
ends inside the class ceiling.
"""

from __future__ import annotations

import asyncio
import functools
import textwrap
from collections.abc import Generator
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
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
from tests.fixtures.task_class_transitions import with_declared_transitions

pytestmark = pytest.mark.unit

CEILING_SECONDS = 240
FLOOR_SECONDS = 45
# How far past its deadline a slow attempt runs before the dispatch's kill
# lands: inside the process-boundary terminate grace.
OVERRUN_SECONDS = 0.5

_ROUTING_TIERS_YAML = textwrap.dedent(
    """\
    tiers:
      - name: local
        cost_per_1k_tokens: 0.0
        models:
          - id: local-a-model
            backend_id: local-a
            max_context_tokens: 8192
            use_for: [document]
          - id: local-b-model
            backend_id: local-b
            max_context_tokens: 8192
            use_for: [document]
          - id: local-c-model
            backend_id: local-c
            max_context_tokens: 8192
            use_for: [document]
        eval_before_accept: false
        max_retries: 0
      - name: cheap_cloud
        cost_per_1k_tokens: 0.001
        models:
          - id: cloud-2-model
            backend_id: cloud-2
            max_context_tokens: 8192
            use_for: [document]
        eval_before_accept: false
        max_retries: 0
      - name: mid_cloud
        cost_per_1k_tokens: 0.002
        models:
          - id: cloud-3-model
            backend_id: cloud-3
            max_context_tokens: 8192
            use_for: [document]
        eval_before_accept: false
        max_retries: 0
      - name: top_cloud
        cost_per_1k_tokens: 0.003
        models:
          - id: cloud-4-model
            backend_id: cloud-4
            max_context_tokens: 8192
            use_for: [document]
        eval_before_accept: false
        max_retries: 0
    """
)


def _backend(backend_id: str, provider: str, tier: str, timeout_ms: int) -> str:
    host = (
        "http://198.51.100.10:9000"
        if provider == "local"
        else f"https://{backend_id}.test"
    )
    return textwrap.dedent(
        f"""\
          - backend_id: {backend_id}
            provider: {provider}
            endpoint_url: "{host}/v1/chat/completions"
            model_name: {backend_id}-model
            tier: {tier}
            timeout_ms: {timeout_ms}
            max_tokens: 4096
            capabilities: [document]
        """
    )


_BACKEND_IDS = ["local-a", "local-b", "local-c", "cloud-2", "cloud-3", "cloud-4"]

_BIFROST_YAML = (
    textwrap.dedent(
        """\
        config_version: "1.0.0"
        schema_version: "bifrost_delegation.v1"
        backends:
        """
    )
    + _backend("local-a", "local", "local", 30000)
    + _backend("local-b", "local", "local", 30000)
    + _backend("local-c", "local", "local", 30000)
    + _backend("cloud-2", "glm", "frontier_api", 120000)
    + _backend("cloud-3", "glm", "frontier_api", 120000)
    + _backend("cloud-4", "glm", "frontier_api", 120000)
    + textwrap.dedent(
        f"""\
        routing_rules:
          - rule_id: "7770b87c-9dc5-508d-9ee7-d7ac15acdfeb"
            priority: 10
            task_class: document
            task_class_contract_version: "1.0.0"
            backend_policy_version: "1.0.0"
            match_operation_types: [chat_completion]
            match_capabilities: [document]
            backend_ids: [{", ".join(_BACKEND_IDS)}]
            fallback_policy:
              action: escalate_to_next_tier
              max_retries: 1
              on_exhaust: return_error
            shadow_policy_id: "9f0bcb8c-c33e-5016-a33a-f41a54b04c2b"
        default_backends:
          - local-a
        circuit_breaker:
          failure_threshold: 50
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
)

_TASK_CLASS_CONTRACT_YAML = textwrap.dedent(
    f"""\
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
          max_escalations: 3
          tier_order:
            - local
            - cheap_cloud
            - mid_cloud
            - top_cloud
    execution_budgets:
      document: {{task_class_timeout_ceiling_seconds: {CEILING_SECONDS}, terminal_delivery_margin_seconds: 60, rung_floor_seconds: {FLOOR_SECONDS}}}
    """
)

_DOCUMENT_START_MARKER = resolve_task_class_output_contract("document").start_marker
ANSWER = (
    f"{_DOCUMENT_START_MARKER}\n"
    "The ladder keeps time in reserve for every rung after the one running. "
    "So a slow rung cannot spend the time the last rung needs to answer."
)


@pytest.fixture
def _fixture_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """Bind routing, bifrost and task-class config to the fixture YAML above."""
    tiers_path = tmp_path / "routing_tiers.yaml"
    tiers_path.write_text(_ROUTING_TIERS_YAML)
    bifrost_path = tmp_path / "bifrost_delegation.yaml"
    bifrost_path.write_text(_BIFROST_YAML)
    contract_path = tmp_path / "task_class_contracts.v1.yaml"
    contract_path.write_text(with_declared_transitions(_TASK_CLASS_CONTRACT_YAML))

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


class _Clock:
    """A monotonic clock the test moves; a slow rung costs no wall time."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _SlowUntilTheLastRung:
    """Every backend but cloud-4 runs past the deadline it is given; cloud-4 answers.

    A slow call is killed by the dispatch at its timeout plus the dispatch
    buffer; this fake moves the clock that far and a little further, then
    returns the timeout the kill produces.
    """

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.calls: list[tuple[str, float, float]] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(
            (request.provider, self.clock.now, float(request.timeout_seconds))
        )
        if request.provider == "cloud-4":
            return ModelLlmDelegationCallResult(
                request_id=request.request_id, success=True, content=ANSWER
            )
        self.clock.now += (
            float(request.timeout_seconds)
            + port_mod._DISPATCH_TIMEOUT_BUFFER_SECONDS
            + OVERRUN_SECONDS
        )
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=False,
            failure_class=EnumDelegationFailureClass.TIMEOUT,
            error_message="killed at the dispatch deadline",
        )


def _dispatch(port: LocalDelegationDispatchPort) -> dict[str, Any]:
    budget = resolve_task_class_execution_budget("document")
    return asyncio.run(
        port.dispatch(
            prompt="Explain why the delegation ladder keeps time in reserve.",
            task_type="document",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=budget.task_class_timeout_ceiling_seconds,
            terminal_delivery_margin_seconds=budget.terminal_delivery_margin_seconds,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    )


@pytest.mark.usefixtures("_fixture_env")
def test_three_slow_rungs_leave_the_last_rung_its_floor(tmp_path: Path) -> None:
    budget = resolve_task_class_execution_budget("document")
    assert budget.task_class_timeout_ceiling_seconds == CEILING_SECONDS
    assert budget.rung_floor_seconds == FLOOR_SECONDS

    clock = _Clock()
    started = clock.now
    effect = _SlowUntilTheLastRung(clock)
    result = _dispatch(
        LocalDelegationDispatchPort(
            effect_handler=effect,
            evidence_db_path=tmp_path / "d.sqlite",
            effect_process_boundary=False,
            clock=clock,
        )
    )

    called = [provider for provider, _, _ in effect.calls]
    # The first rung is tried three times (two retries), every attempt slow;
    # then two more slow rungs; then the last rung.
    assert sorted(called[:3]) == ["local-a", "local-b", "local-c"], called
    assert called[3:] == ["cloud-2", "cloud-3", "cloud-4"], called

    last_provider, last_started, _ = effect.calls[-1]
    assert last_provider == "cloud-4"
    usable = started + CEILING_SECONDS - last_started
    assert usable >= FLOOR_SECONDS, (
        f"the last rung started with {usable:.1f}s of the {CEILING_SECONDS}s "
        f"budget left, under the {FLOOR_SECONDS}s floor: {effect.calls}"
    )

    assert result["status"] == "completed", result
    assert clock.now - started <= CEILING_SECONDS, (
        f"the run ended {clock.now - started:.1f}s after it started, past the "
        f"{CEILING_SECONDS}s class ceiling"
    )

    # Each attempt's timeout is the smaller of its backend's and its rung's
    # window, so every slow rung ends by the chain deadline less one floor per
    # later rung.
    rung_of = {
        "local-a": 0,
        "local-b": 0,
        "local-c": 0,
        "cloud-2": 1,
        "cloud-3": 2,
    }
    backend_timeout = {
        "local-a": 30.0,
        "local-b": 30.0,
        "local-c": 30.0,
        "cloud-2": 120.0,
        "cloud-3": 120.0,
    }
    for provider, at, timeout in effect.calls[:-1]:
        cutoff = started + CEILING_SECONDS - FLOOR_SECONDS * (3 - rung_of[provider])
        assert timeout <= backend_timeout[provider], (provider, timeout)
        ended = at + timeout + port_mod._DISPATCH_TIMEOUT_BUFFER_SECONDS
        assert ended + OVERRUN_SECONDS <= cutoff, (
            f"{provider} could run to {ended + OVERRUN_SECONDS - started:.1f}s, "
            f"past its rung's cutoff at {cutoff - started:.1f}s"
        )


def test_every_shipped_class_declares_its_rung_floor() -> None:
    """The shipped contract declares a floor for every class, inside its ceiling."""
    contract = yaml.safe_load(
        resources.files("omnimarket.configs")
        .joinpath("task_class_contracts.v1.yaml")
        .read_text(encoding="utf-8")
    )
    budgets = contract["execution_budgets"]
    assert budgets
    for task_class, budget in budgets.items():
        floor = budget.get("rung_floor_seconds")
        assert isinstance(floor, int), task_class
        assert floor >= 1, task_class
        assert floor <= budget["task_class_timeout_ceiling_seconds"], task_class
