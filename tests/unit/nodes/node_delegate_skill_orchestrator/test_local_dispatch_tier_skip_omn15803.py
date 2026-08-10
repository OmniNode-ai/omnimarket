# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RED-first regression tests for the OMN-15803 tier-skip routing defect.

Two independent mechanisms, both reproduced against the REAL routing authority
(``handler_delegation_routing.py``) and the REAL ``LocalDelegationDispatchPort``
dispatch loop — env-bound fixture config files, not a fully-mocked ladder like
``test_local_dispatch_escalation_omn13849.py`` uses. This is deliberate: the
defect lives in the SEAM between the port and the routing authority, so a test
that monkeypatches the routing authority functions away would not exercise it.

Mechanism 1 (tier mislabeling): ``ModelResolvedDelegationBackend.tier`` carries
the bifrost contract's own descriptive ``tier:`` field (e.g. ``frontier_api``),
a DIFFERENT vocabulary than the routing-authority tier_order name
(``tier_for_backend()``). Several ``port_local_delegation_dispatch.py`` sites
read ``.tier`` directly instead of re-deriving the routing-authority name, so
``attempts[]`` and the outbound ``model_tier`` wire field surface the wrong
label — a label that is not even a member of the task class's declared
``tier_order``.

Mechanism 2 (escalation-to-identical-backend): when two DIFFERENT routing
tiers (e.g. ``cheap_cloud`` and ``claude``) declare the SAME bifrost
``backend_id`` for a task type, escalating "up" from one to the other
re-resolves the IDENTICAL backend+model — a functional no-op. The routing
authority already has the fix (``next_eligible_tier(...,
excluded_backend_refs=...)``, built for OMN-15503), but
``LocalDelegationDispatchPort._resolve_next_backend`` never threads
``excluded_backend_refs`` into its call, so the bus-less local CLI path never
benefits from it.

Fixture shape: ``local`` tier's only backend is genuinely distinct
(``local-x``); ``cheap_cloud`` and ``claude`` both declare ``backend_id:
cloud-shared`` — reproducing the live ``routing_tiers.yaml`` shape where
``cloud-gemini-pro`` backs both tiers for ``research``. ``cloud-shared``'s
bifrost-declared ``tier:`` field is ``frontier_api`` (matching the live
``cloud-gemini-pro`` entry), NOT ``cheap_cloud``/``claude`` — this is what lets
the mislabeling assertion be unambiguous.
"""

from __future__ import annotations

import asyncio
import functools
import textwrap
from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
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
          - id: shared-model
            backend_id: cloud-shared
            max_context_tokens: 8192
            use_for: [research]
        eval_before_accept: false
        max_retries: 0
      - name: claude
        cost_per_1k_tokens: 0.002
        models:
          - id: shared-model
            backend_id: cloud-shared
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
        endpoint_url: "http://198.51.100.10:9000/v1/chat/completions"
        model_name: local-model
        tier: local
        timeout_ms: 30000
        max_tokens: 4096
        capabilities: [research]
      - backend_id: cloud-shared
        endpoint_url: "https://cloud.test/v1/chat/completions"
        model_name: shared-model
        # OMN-15803 mechanism 1: the bifrost contract's own descriptive tier
        # label, deliberately distinct from any routing_tiers.yaml tier name —
        # matches the live cloud-gemini-pro entry's "tier: frontier_api".
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
        backend_ids: [local-x, cloud-shared]
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
          heuristic: []
        escalation_policy:
          max_escalations: 2
          tier_order:
            - local
            - cheap_cloud
            - claude
    """
)

# Every tier's routing name the fixture's research tier_order declares — used
# to assert invariant (ii): a resolved/reported tier is always a member of
# this set, never a raw bifrost label like "frontier_api".
_DECLARED_TIER_ORDER = frozenset({"local", "cheap_cloud", "claude"})


@pytest.fixture
def _fixture_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """Bind DELEGATION_ROUTING_TIERS_PATH/BIFROST_CONTRACT_PATH/TASK_CLASS_CONTRACT_PATH
    to the fixture YAML above and clear every routing-authority module cache
    before and after — mirrors the OMN-15628 fail-fast test's cache-clear
    pattern (``test_omn15628_fail_fast_config_bindings.py``).
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

    # ``omnimarket.routing.delegation_backend_resolution.resolve_delegation_backend``
    # (the backend_id-targeted re-resolution every escalation hop uses) is a
    # SEPARATE loader from ``handler_delegation_routing._load_bifrost_endpoints``
    # — its ``config_path``/``overlay_path`` are keyword defaults bound at
    # function-definition time, so patching the module-level path constants
    # after import has no effect on the already-bound defaults. Patch the
    # PORT's imported name to the REAL function with the SAME fixture file (and
    # a nonexistent dev-overlay path, so this test never reads the real host's
    # ``~/.omninode/delegation/bifrost_overrides.yaml``) pre-bound via
    # ``functools.partial`` — the real resolution/validation logic still runs,
    # only the config source is redirected, so both loaders agree on one
    # fixture instead of silently diverging.
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


class _AlwaysTransportFailEffect:
    """Every call fails transport with a RETRYABLE failure_class.

    Records each call's ``(backend_id, model_id, model_tier)`` so the test can
    assert on both the escalation path AND the wire-level tier label posted to
    the effect (mechanism 1's second, cost-accounting-facing symptom).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append((request.provider, request.model_id, request.model_tier))
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=False,
            failure_class=EnumDelegationFailureClass.MODEL_UNAVAILABLE,
            error_message="connection refused",
        )


def _dispatch(
    port: LocalDelegationDispatchPort, *, correlation_id
) -> dict[str, object]:
    return asyncio.run(
        port.dispatch(
            prompt="explain the tradeoff",
            task_type="research",
            correlation_id=correlation_id,
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    )


@pytest.mark.usefixtures("_fixture_env")
def test_attempt_tier_labels_are_routing_authority_members_not_bifrost_labels(
    tmp_path: Path,
) -> None:
    """Invariant (ii): every attempts[] "tier" is a declared tier_order member.

    RED at current head: the cheap_cloud attempt (backend cloud-shared, whose
    bifrost contract entry declares ``tier: frontier_api``) surfaces
    ``attempts[1]["tier"] == "frontier_api"`` — not a member of research's
    declared ``tier_order`` at all. GREEN after the fix: it reads
    "cheap_cloud", the routing-authority name that was actually walked.
    """
    effect = _AlwaysTransportFailEffect()
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, correlation_id=uuid4())

    attempts = result["attempts"]
    assert isinstance(attempts, list)
    assert len(attempts) >= 2, "expected at least local + cheap_cloud attempts"

    reported_tiers = [a["tier"] for a in attempts]
    for tier in reported_tiers:
        assert tier in _DECLARED_TIER_ORDER, (
            f"attempts[] reported tier={tier!r}, not a member of research's "
            f"declared tier_order {sorted(_DECLARED_TIER_ORDER)} — this is the "
            "bifrost-contract 'frontier_api' label leaking into the receipt "
            "instead of the routing-authority tier name."
        )

    # The cheap_cloud attempt specifically must be labeled "cheap_cloud", not
    # the bifrost backend's own "frontier_api" field.
    assert reported_tiers[1] == "cheap_cloud"

    # Mechanism 1's second symptom: the wire-level model_tier posted to the
    # effect handler for the cheap_cloud call must also be the routing tier
    # name — a wrong value here silently mis-prices the call downstream
    # (handler_llm_delegation_call.py's _get_tier_price_per_1m/_FALLBACK_PRICE_PER_1M
    # are keyed on routing_tiers.yaml vocabulary, not bifrost labels).
    assert effect.calls[1][2] == "cheap_cloud"


@pytest.mark.usefixtures("_fixture_env")
def test_escalation_never_reattempts_the_identical_backend(
    tmp_path: Path,
) -> None:
    """Invariant (iii): an escalation step must change backend or tier.

    RED at current head: cheap_cloud and claude both resolve to backend_id
    "cloud-shared" for research (mirrors the live cloud-gemini-pro shape) —
    escalating cheap_cloud -> claude re-dispatches the IDENTICAL backend+model
    a third time, a functional no-op with zero new information. GREEN after
    the fix: the port threads its accumulated ``excluded_backend_refs`` into
    ``next_eligible_tier``, which correctly reports the ladder exhausted after
    cheap_cloud (claude offers no genuinely different backend) — exactly 2
    attempts (local, cheap_cloud), not 3.
    """
    effect = _AlwaysTransportFailEffect()
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, correlation_id=uuid4())

    attempts = result["attempts"]
    assert isinstance(attempts, list)
    seen_backend_model_pairs: list[tuple[object, object]] = [
        (a["backend_id"], a["model_id"]) for a in attempts
    ]
    assert len(seen_backend_model_pairs) == len(set(seen_backend_model_pairs)), (
        "escalation re-dispatched an identical (backend_id, model_id) pair "
        f"already attempted: {seen_backend_model_pairs} — an escalation step "
        "must change backend or tier, never repeat the exact same call."
    )

    # The ladder is genuinely exhausted after local + cheap_cloud: claude
    # offers no backend distinct from cheap_cloud's, so it must never be
    # separately attempted.
    assert len(attempts) == 2
    assert result["status"] == "failed"
    assert result["escalation_count"] == 1
