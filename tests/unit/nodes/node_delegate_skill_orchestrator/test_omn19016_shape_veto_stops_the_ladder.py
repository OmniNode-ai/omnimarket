# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19016 — the ladder stops on a veto no rung can satisfy.

The measured run this file reproduces is correlation
``f037b9be-b242-4b83-9912-3e6a5af83e82`` (run
``11dbb397-eb1b-4fa5-b5f2-a70a10e0ae10``, dev lane, deployed locus). The prompt
asked for exactly one word, the model answered ``READY``, and the ladder walked
four rungs — three free re-draws on the local tier and one metered cloud rung —
to four byte-identical refusals::

    local  Qwen3.8-27B   climb  0.867  WEAK_OUTPUT: bare single-word fragment
    local  Qwen3.8-27B   climb  0.867  WEAK_OUTPUT: bare single-word fragment
    local  Qwen3.8-27B   climb  0.867  WEAK_OUTPUT: bare single-word fragment
    cheap  glm-5.3-flash climb  0.867  WEAK_OUTPUT: bare single-word fragment
    terminal: failed, quality_score 0.867, score_vs_bar=at_or_above_bar

Every rung answered, correctly, with the same word, because the refusal is a
function of the response's SHAPE and not of the model's strength. The ladder
exists to buy a better answer from a costlier rung; here there was no better
answer to buy, and one of the rungs it bought was metered.

The fixture below is that ladder: a free local tier with a re-draft budget, a
metered cloud tier above it, and an effect that answers ``READY`` every time —
so a run that walks past the first rung is the defect, and a run that stops on
it is the fix. The negative control uses the same fixture with a genuinely
truncated answer, which a costlier rung CAN cure, and asserts the ladder still
climbs for it.
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

# The measured answer and the measured refusal, from the captured receipt.
LIVE_ANSWER = "READY"

# The two acceptance decisions, as their wire values rather than as members of
# the acceptance-decision enum. That enum is a ``StrEnum``, so a member
# compares equal to its value and nothing is weakened by spelling it out; what
# is gained is that this module still IMPORTS against pre-fix source, where
# the terminate member does not exist yet. An AttributeError raised while
# reaching for a missing member proves only that the member is missing. These
# assertions fail on the measured behaviour instead: the run climbed.
DECISION_TERMINATE = "terminate"
DECISION_CLIMB = "climb"

# A genuinely incomplete answer: the model stopped mid-clause on a function
# word. A costlier rung finishes the sentence, so this one must still climb.
TRUNCATED_ANSWER = "The two caching strategies differ mainly in the"

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
        max_retries: 2
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

# The `document` class as it is declared in production, narrowed to the two
# rules this ladder needs: the deterministic floor the answer passes, and the
# adequacy authority that vetoes it.
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


class _AlwaysAnswers:
    """Every rung answers, with the same text, exactly as the live run did."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append((request.provider, request.model_id))
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=self.answer,
        )


def _dispatch(port: LocalDelegationDispatchPort) -> dict[str, Any]:
    return asyncio.run(
        port.dispatch(
            # A prompt that declares no response shape, so the OMN-16932 shape
            # override does not apply and the class's own adequacy rule is the
            # authority — the state the captured run was in.
            prompt="Summarise the delegation ladder for the runbook.",
            task_type="document",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    )


def _port(effect: _AlwaysAnswers, tmp_path: Path) -> LocalDelegationDispatchPort:
    return LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )


@pytest.mark.usefixtures("_fixture_env")
def test_shape_veto_stops_the_ladder_on_the_first_rung(tmp_path: Path) -> None:
    """RED at f037b9be's head: four rungs, one metered, to the same refusal."""
    effect = _AlwaysAnswers(LIVE_ANSWER)
    result = _dispatch(_port(effect, tmp_path))

    called = [provider for provider, _ in effect.calls]
    assert called == ["local-x"], (
        "the ladder climbed past a veto no rung can satisfy; every rung "
        f"returns the same one-word answer. backends called: {called}"
    )
    assert result["status"] == "failed"
    assert len(result["attempts"]) == 1
    assert result["escalation_count"] == 0
    assert result["cost_usd"] == 0.0


@pytest.mark.usefixtures("_fixture_env")
def test_the_terminal_says_the_ladder_stopped(tmp_path: Path) -> None:
    """The record says TERMINATE, names the rule, and carries a zeroed score.

    A last attempt recorded as ``climb`` on a run that stopped is the same
    infer-it-from-absence defect the decision field was introduced to remove.
    """
    effect = _AlwaysAnswers(LIVE_ANSWER)
    result = _dispatch(_port(effect, tmp_path))

    attempt = result["attempts"][0]
    assert attempt["acceptance_decision"] == DECISION_TERMINATE
    assert "semantic_adequacy" in attempt["acceptance_detail"]
    assert "no_rung_can_satisfy=true" in attempt["acceptance_detail"]
    assert attempt["quality_score"] == 0.0
    assert result["quality_score"] == 0.0
    assert any("SHAPE_REFUSED" in reason for reason in result["quality_gates_failed"])
    # AC1 on this path. The port terminalises with the gate's raw reasons, so
    # no ``score_vs_bar`` token is composed here at all and the invariant holds
    # by there being nothing to contradict. The token IS composed on the
    # orchestrator path, which is the one that produced the captured receipt,
    # and it is asserted there — see
    # ``test_the_composed_terminal_reason_no_longer_claims_at_or_above_bar`` in
    # tests/unit/delegation/test_omn19016_veto_zeroes_score_and_stops_climb.py.
    # Asserted negatively on both paths so a future change that starts
    # composing the token here cannot reintroduce the contradiction unnoticed.
    assert result["quality_gates_failed"], "a failed terminal named no reason"
    for reason in result["quality_gates_failed"]:
        assert "at_or_above_bar" not in reason, reason
    # The answer the model actually produced is still handed back: the ladder
    # stopping is not a reason to discard authored work (OMN-14220).
    assert result["content"] == LIVE_ANSWER


@pytest.mark.usefixtures("_fixture_env")
def test_a_truncated_answer_still_climbs(tmp_path: Path) -> None:
    """Negative control: the ladder keeps escalating what a costlier rung can cure."""
    effect = _AlwaysAnswers(TRUNCATED_ANSWER)
    result = _dispatch(_port(effect, tmp_path))

    called = [provider for provider, _ in effect.calls]
    assert called.count("local-x") > 1, (
        f"the free re-draft budget was skipped for a curable refusal: {called}"
    )
    assert "cloud-x" in called, (
        f"a truncated answer must still reach the higher tier: {called}"
    )
    assert result["status"] == "failed"
    assert all(
        attempt["acceptance_decision"] == DECISION_CLIMB
        for attempt in result["attempts"]
    )
