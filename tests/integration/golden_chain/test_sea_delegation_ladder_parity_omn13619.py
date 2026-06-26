# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""SEA delegation escalation-ladder parity proof (OMN-13619, WS-C Phase 4.3).

The standalone SEA repo (``onex-self-extending-agent``) carried a bespoke
research-harness escalation ladder in
``src/delegation/{executor,config,events,validation}.py``. Phase 4.3 of the
SEA -> canonical migration (epic OMN-13604) routes that capability to the
permanent canonical home in omnimarket:

  * tier config           -> ``configs/routing_tiers.yaml`` (+ task-class overlay
                             ``configs/task_class_contracts.v1.yaml`` + bifrost
                             endpoint overlay) -- NOT a bespoke ``config.py``.
  * escalate/terminate     -> ``node_delegation_escalation_decision_compute``
    decision               (``HandlerEscalationDecision``).
  * next-tier resolution   -> ``node_delegation_routing_reducer``
                             (``next_eligible_tier`` / ``tier_max_retries`` /
                             ``describe_no_higher_tier_available``), which reads
                             the tier config from the contract+overlay.

This module is the parity proof required by the ticket DoD: "escalation ladder
reproduced through the canonical delegation nodes (focused integration/golden-
chain proof of the tier ladder)". Each test below mirrors one behavior of the
SEA ``DelegationExecutor`` (the seven cases in the SEA
``tests/unit/test_delegation_executor.py``) and asserts the canonical nodes
produce the SAME verdict.

SEA behavior -> canonical proof mapping:

  1. succeeds on first tier (no escalation)   -> escalation decision is never
     consulted on success; the ladder's first rung is the routing reducer's
     declaration-order head.
  2. escalates on escalatable failure         -> ``error_retryable=True`` + a
     routable ``next_tier_name`` -> ``can_escalate=True``.
  3. does NOT escalate on non-escalatable     -> ``error_retryable=False`` ->
     failure (e.g. HARDCODED_TOPIC)              terminate, next tier untouched.
  4. terminates on REPLAY_VIOLATION           -> ``error_retryable=False`` with
     a blocks-closeout reason -> terminate.
  5. respects budget guards                   -> ``escalation_count >=
                                                 max_escalation_attempts`` ->
                                                 terminate before a routable tier
                                                 is consulted.
  6. skips disabled tier                      -> ``next_eligible_tier`` skips a
                                                 tier in ``excluded_tiers`` and a
                                                 tier whose backend secret is
                                                 unresolvable (the canonical
                                                 analogue of SEA's
                                                 ``disabled_reason``).
  7. propagates run / correlation ids         -> the routing reducer threads the
                                                 request ``correlation_id`` onto
                                                 the routing decision unchanged.

All tiers, providers, models, endpoints, and keys are resolved from the contract
overlay (``routing_tiers.yaml`` + bifrost), never from env vars, and the
canonical path contains NO shelled CLI (the SEA executor's only out-of-process
call, ``GenerationConsumer``, is replaced by the canonical HTTP inference effect;
OMN-13215 removed the shelled ``codex-cli`` tier from the ceiling).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import (
    ModelDelegationRequest,
    ModelRoutingIntent,
)

from omnimarket.nodes.node_delegation_escalation_decision_compute.handlers.handler_escalation_decision import (
    HandlerEscalationDecision,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    next_eligible_tier,
    tier_max_retries,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    HandlerRoutingIntent,
)
from omnimarket.routing.model_escalation_decision_request import (
    ModelEscalationDecisionRequest,
)

# Bifrost contract with one routable backend (local-coder). The endpoint is a
# COMPLETE URL (OMN-12815) so the routing reducer posts it verbatim with no
# construction. A task only local can serve proves the canonical "skip a tier
# whose backend cannot be resolved" path (the analogue of a SEA disabled tier).
_BIFROST_CONTRACT_PARITY = (
    "config_version: '2.0.0'\n"
    "schema_version: bifrost_delegation.v1\n"
    "backends:\n"
    "  - backend_id: local-coder\n"
    '    endpoint_url: "http://parity-coder:8000/v1/chat/completions"\n'
    '    model_name: "qwen3-coder-30b"\n'
    "    tier: local\n"
    "    timeout_ms: 30000\n"
    "    max_tokens: 8192\n"
    "    capabilities: [research, code_generation]\n"
    "routing_rules:\n"
    '  - rule_id: "11111111-1111-4111-8111-111111111111"\n'
    "    priority: 10\n"
    "    task_class: research\n"
    '    task_class_contract_version: "1.0.0"\n'
    '    backend_policy_version: "2.0.0"\n'
    "    match_operation_types: [chat_completion]\n"
    "    match_capabilities: [research]\n"
    "    backend_ids: [local-coder]\n"
    "    fallback_policy:\n"
    "      action: return_error\n"
    "      max_retries: 0\n"
    "      on_exhaust: return_error\n"
    '    shadow_policy_id: "22222222-2222-4222-8222-222222222222"\n'
    "default_backends:\n"
    "  - local-coder\n"
    "circuit_breaker:\n"
    "  failure_threshold: 5\n"
    "  window_seconds: 30\n"
    "failover:\n"
    "  max_attempts: 1\n"
    "  backoff_base_ms: 500\n"
    "shadow_mode:\n"
    "  enabled: false\n"
    '  policy_version: "test"\n'
    "  log_sample_rate: 1.0\n"
    "  comparison_logging_enabled: true\n"
    "  max_shadow_latency_ms: 5.0\n"
)


def _install_parity_bifrost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the routing reducer at the parity bifrost contract and reset caches."""
    import omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing as routing_module

    routing_module._config = None
    routing_module._load_bifrost_endpoints.cache_clear()
    routing_module._get_task_class_contract.cache_clear()
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_BIFROST_CONTRACT_PARITY, encoding="utf-8")
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))


def _reset_routing_caches() -> None:
    import omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing as routing_module

    routing_module._config = None
    routing_module._load_bifrost_endpoints.cache_clear()
    routing_module._get_task_class_contract.cache_clear()


def _escalation_req(**overrides: object) -> ModelEscalationDecisionRequest:
    base: dict[str, object] = {
        "escalation_count": 0,
        "max_escalation_attempts": 2,
        "current_tier_name": "local",
        "error_retryable": True,
        "next_tier_name": "cheap_cloud",
        "non_retryable_reason": "non_retryable_inference_response",
        "no_higher_tier_reason": None,
    }
    base.update(overrides)
    return ModelEscalationDecisionRequest.model_validate(base)


# ---------------------------------------------------------------------------
# 1. Tier config lives in the contract overlay (NOT a bespoke config file).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTierConfigFromContractOverlay:
    """The escalation ladder's tiers/budgets come from routing_tiers.yaml.

    SEA's ``config.py`` (``load_default_config`` / ``ModelEscalationConfig``)
    built the tier ladder imperatively in Python. The canonical home reads it
    from the declarative contract overlay so the ladder is swappable by editing
    YAML -- no code change. This proves the "tier config in contract overlay"
    DoD.
    """

    def test_routing_tiers_yaml_is_the_tier_config_authority(self) -> None:
        from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
            handler_delegation_routing as routing_module,
        )

        config_path = routing_module._DEFAULT_CONFIG_PATH
        assert config_path.name == "routing_tiers.yaml"
        assert config_path.exists(), (
            "The canonical tier config must be a contract overlay file, "
            "not a bespoke Python config module."
        )

    def test_per_tier_retry_budget_resolved_from_overlay(self) -> None:
        # SEA: ModelEscalationTier.max_retries lived in config.py.
        # Canonical: tier_max_retries reads max_retries from routing_tiers.yaml.
        assert tier_max_retries("local") >= 0
        assert tier_max_retries("cheap_cloud") >= 0
        assert tier_max_retries("claude") >= 0

    def test_unknown_tier_retry_budget_fails_fast(self) -> None:
        # No silent default (CLAUDE.md rule 8): unknown tier raises.
        with pytest.raises(ValueError, match=r"not declared in routing_tiers\.yaml"):
            tier_max_retries("tier-that-does-not-exist")


# ---------------------------------------------------------------------------
# SEA DelegationExecutor behavior parity (the seven executor unit cases).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSeaExecutorLadderParity:
    """Each test mirrors one SEA DelegationExecutor behavior on the canonical nodes."""

    def test_1_first_tier_head_is_routing_reducer_declaration_head(self) -> None:
        """SEA: succeeds on first tier with escalation_count == 0.

        Canonical: the ladder head is the routing reducer's first declared tier;
        a success never consults the escalation-decision COMPUTE (it is only
        reached on failure). We assert the head has a real second rung -- i.e.
        the ladder is wired, not a single point.
        """
        head_next = next_eligible_tier("local", frozenset())
        assert head_next is not None, (
            "the canonical ladder must have a second rung after the local head, "
            "mirroring SEA's local -> next-tier escalation"
        )

    def test_2_escalates_on_escalatable_failure(self) -> None:
        """SEA: SCHEMA_VIOLATION (retryable + escalatable) -> escalate to next tier."""
        result = HandlerEscalationDecision().handle(
            _escalation_req(
                error_retryable=True,
                current_tier_name="local",
                next_tier_name="cheap_cloud",
            )
        )
        assert result.can_escalate is True
        assert result.next_tier_name == "cheap_cloud"
        assert result.terminal_failure_reason is None

    def test_3_does_not_escalate_on_non_escalatable_failure(self) -> None:
        """SEA: HARDCODED_TOPIC (not escalatable) -> terminate, next tier untouched."""
        result = HandlerEscalationDecision().handle(
            _escalation_req(
                error_retryable=False,
                non_retryable_reason="non_escalatable_failure_class",
                next_tier_name="cheap_cloud",
            )
        )
        assert result.can_escalate is False
        assert result.next_tier_name is None
        assert result.terminal_failure_reason == "non_escalatable_failure_class"

    def test_4_terminates_on_replay_violation(self) -> None:
        """SEA: REPLAY_VIOLATION blocks closeout -> terminate, never escalate."""
        result = HandlerEscalationDecision().handle(
            _escalation_req(
                error_retryable=False,
                non_retryable_reason="replay_violation_blocks_closeout",
                escalation_count=0,
                max_escalation_attempts=99,
                next_tier_name="claude",
            )
        )
        assert result.can_escalate is False
        assert result.terminal_failure_reason == "replay_violation_blocks_closeout"

    def test_5_respects_budget_guard(self) -> None:
        """SEA: budget reached -> skip remaining tiers.

        Canonical: ``escalation_count >= max_escalation_attempts`` terminates
        even when a routable next tier is available (budget check precedes tier
        resolution), matching SEA's ``max_total_attempts`` / cost guard.
        """
        result = HandlerEscalationDecision().handle(
            _escalation_req(
                escalation_count=2,
                max_escalation_attempts=2,
                next_tier_name="claude",
            )
        )
        assert result.can_escalate is False
        assert result.terminal_failure_reason == "max_escalation_attempts_reached"
        assert result.next_tier_name is None

    def test_6a_skips_excluded_tier(self) -> None:
        """SEA: disabled tier is skipped.

        Canonical analogue #1: a tier named in ``excluded_tiers`` is skipped by
        ``next_eligible_tier`` -- the ladder advances past it to the next rung.
        """
        skipped = next_eligible_tier("local", frozenset({"cheap_cloud"}))
        not_skipped = next_eligible_tier("local", frozenset())
        assert skipped is not None
        assert skipped != "cheap_cloud"
        assert not_skipped == "cheap_cloud"

    def test_6b_skips_tier_with_unresolvable_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SEA: disabled tier is skipped.

        Canonical analogue #2: with the parity bifrost contract declaring ONLY
        the local-coder backend, escalating past local for a task that only
        local can serve yields no routable higher tier -- the reducer reports
        ladder exhaustion instead of routing to a tier whose backend it cannot
        resolve (the canonical equivalent of SEA's ``disabled_reason`` skip).
        """
        _install_parity_bifrost(tmp_path, monkeypatch)
        try:
            higher = next_eligible_tier("local", frozenset(), task_type="research")
            assert higher is None, (
                "tiers whose backend secret/endpoint cannot be resolved must be "
                "skipped, mirroring SEA's disabled-tier skip"
            )
        finally:
            _reset_routing_caches()

    def test_7_propagates_correlation_id_through_routing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SEA: executor propagates run_id/correlation_id onto the result.

        Canonical: the routing reducer threads the request correlation_id onto
        the routing decision unchanged, and resolves endpoint+model+key from the
        contract overlay (NOT env vars).
        """
        _install_parity_bifrost(tmp_path, monkeypatch)
        correlation_id = uuid4()
        try:
            request = ModelDelegationRequest(
                prompt="Generate a tiny ONEX node.",
                task_type="research",
                correlation_id=correlation_id,
                max_tokens=512,
                emitted_at=datetime.now(UTC),
            )
            decision = HandlerRoutingIntent().handle(
                ModelRoutingIntent(payload=request)
            )
            assert decision.correlation_id == correlation_id
            assert (
                decision.endpoint_url == "http://parity-coder:8000/v1/chat/completions"
            )
            assert decision.selected_model
            assert decision.tier_name == "local"
        finally:
            _reset_routing_caches()


# ---------------------------------------------------------------------------
# No shelled CLI in the canonical delegation path (OMN-13215 + DoD).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNoShelledCliInDelegationPath:
    """The canonical ladder executes through HTTP inference, never a shelled CLI.

    SEA's executor shelled out to a ``GenerationConsumer`` and the ceiling tier
    once shelled ``codex-cli``. OMN-13215 removed the shelled CLI tiers; the
    ceiling now routes through the same HTTP inference path as every lower tier.
    This guard asserts the routing reducer source contains no subprocess / CLI
    shell-out -- the routing decision is pure (endpoint + model + key resolved
    from the contract), and inference happens in the EFFECT node over HTTP.
    """

    def test_routing_reducer_imports_no_subprocess(self) -> None:
        """No subprocess import in the routing module's AST -- not just prose.

        Comments in the source intentionally NAME the removed shelled tiers
        (``codex-cli`` / ``cli_agents``) to document OMN-13215, so a substring
        scan over the whole file is the wrong check. We parse the module AST and
        assert no ``subprocess`` / ``os.system`` style executable import or call
        exists -- the routing decision is pure config resolution; inference
        happens over HTTP in the EFFECT node, never via a shelled CLI.
        """
        import ast

        from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
            handler_delegation_routing as routing_module,
        )

        tree = ast.parse(Path(routing_module.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "subprocess" not in imported, (
            "canonical routing path must not import subprocess (no shelled CLI)"
        )

        # No os.system / Popen attribute call anywhere in the module.
        forbidden_calls: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in {
                    "system",
                    "Popen",
                    "run",
                    "call",
                    "check_output",
                }:
                    value = func.value
                    if isinstance(value, ast.Name) and value.id in {
                        "os",
                        "subprocess",
                    }:
                        forbidden_calls.append(f"{value.id}.{func.attr}")
        assert not forbidden_calls, (
            f"canonical routing path must not shell out: {forbidden_calls}"
        )
