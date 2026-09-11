# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17372 — the BYOK routing overlay must not drop the acceptance contract.

The tenant-overlay branch of ``delta()`` returns BEFORE the single
``resolve_task_class_dod_resolution`` call the platform branch reads, so the
``ModelRoutingDecision`` it builds carries the model defaults for all five DoD
fields: ``dod_deterministic=()``, ``dod_heuristic=()``,
``requested_shape=UNCONSTRAINED``. The orchestrator copies those two empty
tuples into ``ModelQualityGateInput``, ``handler_quality_gate.delta`` sees
``has_contract_dod`` False, and every keyed customer delegation is graded by
``_run_legacy_checks`` — a hardcoded fallback that applies a 60-character floor
no contract declares and then appends ``_NO_ADEQUACY_AUTHORITY_REASON``,
failing the run unconditionally.

OMN-15631 v1(a) designed the overlay to wholesale-replace the platform-resolved
BACKEND, and routing STRUCTURE (tier ladder, escalation, ROI, pricing ceiling)
stays platform-fixed. The ACCEPTANCE contract is neither: it is what counts as
done, it is resolved from the task-class contract for every surface, and it is
never overlay-controllable. The overlay decides WHERE the work runs and WHOSE
key pays; it must not decide WHAT counts as done.

The two live rows this replays, both under tenant
``af432b87-4390-4c76-af97-ee9e158936e7`` whose overlay row is
``task_type='*'`` (so the overlay branch is taken for EVERY task type):

* ``40ac8467-223c-44b7-8477-49a5d711c358`` — scored exactly 1.000 against a
  0.800 bar and was failed anyway on ``TASK_MISMATCH: no deterministic
  acceptance or judge adequacy authority``.
* ``5ad9b033-1e20-4481-af17-dbc1f8c133ce`` — the one-word answer "OMNINODE" to
  an ``exact_literal``-shaped prompt, failed on ``WEAK_OUTPUT: response length
  8 below minimum 60`` at exactly 0.600.

Both numbers are reproducible only from the legacy branch: with
``_WEIGHT_LENGTH=0.4 / _WEIGHT_NO_REFUSAL=0.3 / _WEIGHT_MARKERS=0.3`` and
``summarization`` absent from both ``_TASK_MARKERS`` and ``_MIN_LENGTHS``,
0.4+0.3+0.3 = 1.000 and 0.0+0.3+0.3 = 0.600. "minimum 60" appears in no
contract — it is ``ModelQualityGateInput.min_response_length``'s default.

Test double: ``InmemoryDatabaseAdapter`` + ``resolve_tenant_overlay``, exactly
as ``test_omn15631_tenant_overlay_routing.py`` seeds an overlay row — a pure
DATA write against the interface the routing reducer's I/O boundary reads
through.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.enums.enum_requested_response_shape import EnumRequestedResponseShape
from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateInput
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers import (
    handler_quality_gate,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _NO_ADEQUACY_AUTHORITY_REASON,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    TENANT_OVERLAY_TIER_NAME,
    delta,
    resolve_task_class_dod_resolution,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.routing.customer_key_terminus import EnumDelegationSurface
from omnimarket.routing.tenant_overlay_resolver import (
    TENANT_OVERLAY_TABLE,
    resolve_tenant_overlay,
)

# The live BYOK tenant and its overlay row, replayed verbatim from
# omnidash_analytics.delegation_routing_tenant_overlay (SSM SELECT, 2026-09-07).
_TENANT_ID = "af432b87-4390-4c76-af97-ee9e158936e7"
_BYOK_ALL_TASK_TYPES = "*"
_SECRET_REF = "cred_af432b87-4390-4c76-af97-ee9e158936e7_openrouter_2d3d6ff6583640c293df799cb47215b2"

# workflow 40ac8467's prompt and its response, byte-for-byte from
# .../delegations/40ac8467-223c-44b7-8477-49a5d711c358/{run.json,result.txt}.
_SUMMARY_PROMPT = (
    "Summarize, in three or four complete sentences, what a tenant-scoped "
    "bring-your-own-key routing overlay does in a multi-tenant inference "
    "gateway and why the tenant identifier it is keyed on must be immutable."
)
_SUMMARY_RESPONSE = (
    "A tenant-scoped bring-your-own-key (BYOK) routing overlay allows each "
    "tenant in a multi-tenant inference gateway to supply and control their "
    "own encryption keys, ensuring that data-plane traffic—such as model "
    "weights, prompts, and responses—is encrypted and decrypted "
    # RUF001 suppressed on these two lines ONLY: the curly apostrophe is what
    # the model actually emitted, and this literal is the response byte-for-byte
    # as the gate graded it. Normalising it would make the replay a paraphrase.
    "exclusively with that tenant’s key material. The overlay intercepts "  # noqa: RUF001
    "requests, derives the appropriate key from the tenant’s key store, "  # noqa: RUF001
    "and applies envelope encryption so that the gateway infrastructure never "
    "sees plaintext payloads. Because the tenant identifier is the sole anchor "
    "used to look up and bind the correct key hierarchy, it must be "
    "immutable; any change would break the cryptographic binding, potentially "
    "routing traffic to the wrong key store or allowing a tenant to "
    "masquerade as another, thereby violating isolation and audit guarantees."
)

# workflow 5ad9b033's prompt and its response, same provenance.
_EXACT_LITERAL_PROMPT = "Reply with exactly the word: OMNINODE"
_EXACT_LITERAL_RESPONSE = "OMNINODE"


# A self-contained platform-default contract, so the platform half of the parity
# assertion below never depends on the packaged contract or on
# ``_load_bifrost_endpoints``' process cache. Mirrors the identically-named
# fixture in ``test_omn15631_tenant_overlay_routing.py``.
_BIFROST_ONE_TIER = textwrap.dedent(
    """\
    config_version: "2.0.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-coder
        provider: local
        endpoint_url: "http://local.test:8000/v1/chat/completions"
        model_name: qwen-coder
        tier: local
        timeout_ms: 30000
        max_tokens: 8192
        capabilities: [code_generation]
    routing_rules:
      - rule_id: "c0ffee00-0011-4000-8000-000000000001"
        priority: 10
        task_class: code_generation
        task_class_contract_version: "1.0.0"
        backend_policy_version: "2.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [code_generation]
        backend_ids: [local-coder]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "c0ffee00-0012-4000-8000-000000000001"
    default_backends:
      - local-coder
    circuit_breaker:
      failure_threshold: 5
      window_seconds: 30
    failover:
      max_attempts: 3
      backoff_base_ms: 500
    shadow_mode:
      enabled: false
      policy_version: "test"
      log_sample_rate: 1.0
      comparison_logging_enabled: true
      max_shadow_latency_ms: 5.0
    """
)


@pytest.fixture
def platform_default_routable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """Point routing at a self-contained platform-default contract."""
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_BIFROST_ONE_TIER)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    routing._load_bifrost_endpoints.cache_clear()
    try:
        yield
    finally:
        routing._load_bifrost_endpoints.cache_clear()


def _seed_byok_overlay(db: InmemoryDatabaseAdapter) -> None:
    """Seed the live tenant's overlay row — ``task_type='*'``, as on the cluster."""
    db.upsert(
        TENANT_OVERLAY_TABLE,
        "tenant_id,task_type",
        {
            "tenant_id": _TENANT_ID,
            "task_type": _BYOK_ALL_TASK_TYPES,
            "backend_id": "byok-openrouter",
            "provider": "openrouter",
            "endpoint_url": "https://openrouter.ai/api/v1/chat/completions",
            "model_name": "nvidia/nemotron-3-ultra-550b-a55b:free",
            "secret_ref": _SECRET_REF,
            "timeout_ms": None,
            "max_tokens": None,
        },
    )


def _request(
    *, task_type: str, prompt: str, tenant_id: str | None = _TENANT_ID
) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        correlation_id=uuid4(),
        task_type=task_type,  # type: ignore[arg-type]
        prompt=prompt,
        emitted_at=datetime.now(tz=UTC),
        tenant_id=tenant_id,
    )


def _overlay_decision(*, task_type: str, prompt: str):
    """Route the live BYOK tenant through the overlay branch on the cloud surface."""
    db = InmemoryDatabaseAdapter()
    _seed_byok_overlay(db)
    overlay = resolve_tenant_overlay(db, tenant_id=_TENANT_ID, task_type=task_type)
    assert overlay is not None, "overlay row must resolve via the '*' sentinel retry"
    return delta(
        _request(task_type=task_type, prompt=prompt),
        tenant_overlay=overlay,
        surface=EnumDelegationSurface.CLOUD,
    )


def _gate_input_from(decision, *, response: str) -> ModelQualityGateInput:
    """Copy the routing decision's DoD bands onto the gate input.

    This mirrors ``handler_delegation_workflow`` lines 809 and 1894, which are
    the only two producers of ``ModelQualityGateInput`` on the inference path.
    """
    return ModelQualityGateInput(
        correlation_id=decision.correlation_id,
        task_type=decision.task_type,
        llm_response_content=response,
        dod_deterministic=decision.dod_deterministic,
        dod_heuristic=decision.dod_heuristic,
    )


# --- 1. The routing seam --------------------------------------------------------


@pytest.mark.unit
def test_overlay_decision_carries_the_task_class_dod_bands() -> None:
    """The overlay decision must resolve the SAME acceptance contract as the platform.

    RED before the fix: both bands are ``()`` because ``_decision_from_tenant_overlay``
    never receives a resolution.
    """
    decision = _overlay_decision(task_type="summarization", prompt=_SUMMARY_PROMPT)

    assert decision.tier_name == TENANT_OVERLAY_TIER_NAME
    assert decision.selected_model == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert decision.dod_deterministic == ("response_non_empty",)
    assert "semantic_adequacy" in decision.dod_heuristic


# --- 2. The live failure: score 1.000, failed on TASK_MISMATCH ------------------


@pytest.mark.unit
def test_perfect_scoring_keyed_delegation_passes_the_gate() -> None:
    """Replay of workflow 40ac8467 end to end: route, then grade the real answer.

    RED before the fix: ``passed=False``, ``quality_score=1.0``,
    ``failure_reasons == (_NO_ADEQUACY_AUTHORITY_REASON,)`` — the exact terminal
    string the live row carries.
    """
    decision = _overlay_decision(task_type="summarization", prompt=_SUMMARY_PROMPT)
    result = handler_quality_gate.delta(
        _gate_input_from(decision, response=_SUMMARY_RESPONSE)
    )

    assert _NO_ADEQUACY_AUTHORITY_REASON not in result.failure_reasons
    assert result.failure_reasons == ()
    assert result.passed is True


# --- 3. The other live failure: the exact_literal shape half ---------------------


@pytest.mark.unit
def test_exact_literal_shape_override_reaches_the_overlay_route() -> None:
    """Replay of workflow 5ad9b033: a correct one-word answer must terminalize.

    RED before the fix: the legacy branch emits
    ``WEAK_OUTPUT: response length 8 below minimum 60`` at exactly 0.600 — the
    blunt character floor OMN-13218 removed from ``summarization`` and OMN-16932
    removed again via ``default_shape_overrides.exact_literal``. That whole shape
    machinery is dark on the BYOK path.
    """
    decision = _overlay_decision(
        task_type="summarization", prompt=_EXACT_LITERAL_PROMPT
    )

    assert decision.requested_shape is EnumRequestedResponseShape.EXACT_LITERAL
    assert decision.dod_heuristic == ("no_refusal", "short_form_adequacy")

    result = handler_quality_gate.delta(
        _gate_input_from(decision, response=_EXACT_LITERAL_RESPONSE)
    )
    assert result.passed is True
    assert not any("below minimum 60" in reason for reason in result.failure_reasons)


# --- 4. Parity — this must stay green forever ------------------------------------


_DOD_FIELDS = (
    "dod_deterministic",
    "dod_heuristic",
    "requested_shape",
    "dod_deterministic_source",
    "dod_heuristic_source",
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("task_type", "prompt"),
    [
        ("summarization", _SUMMARY_PROMPT),
        ("summarization", _EXACT_LITERAL_PROMPT),
    ],
)
def test_overlay_decision_equals_the_shared_resolver(
    task_type: str, prompt: str
) -> None:
    """The overlay decision's five DoD fields ARE the shared resolver's output.

    ``resolve_task_class_dod_resolution`` is the one authority both routing
    branches read (OMN-17765 made it one; OMN-17372 hoisted the single call
    above the overlay branch). Asserting the overlay decision equals it directly
    is hermetic — no tier ladder, no bifrost endpoint, nothing ambient — so this
    stays a statement about the seam rather than about the fixture.
    """
    resolution = resolve_task_class_dod_resolution(task_type, prompt)
    decision = _overlay_decision(task_type=task_type, prompt=prompt)

    assert decision.tier_name == TENANT_OVERLAY_TIER_NAME
    assert decision.dod_deterministic == resolution.deterministic
    assert decision.dod_heuristic == resolution.heuristic
    assert decision.requested_shape is resolution.requested_shape
    assert decision.dod_deterministic_source is resolution.deterministic_source
    assert decision.dod_heuristic_source is resolution.heuristic_source


@pytest.mark.unit
@pytest.mark.usefixtures("platform_default_routable")
@pytest.mark.parametrize(
    "prompt",
    ["Write a function that adds two integers." + " x" * 60, _EXACT_LITERAL_PROMPT],
)
def test_overlay_and_platform_resolve_identical_dod(prompt: str) -> None:
    """The two routing sites' five DoD fields agree for the same (task_type, prompt).

    One assertion that makes a future re-divergence of the two sites impossible
    to land silently — the failure this ticket fixes was exactly such a
    divergence, invisible because no test compared the sites.

    Bound to ``code_generation`` under this module's own one-tier bifrost
    contract, and to the customer-local surface, for two reasons that are both
    about hermeticity rather than preference: a keyless CUSTOMER on the cloud is
    a typed refusal (OMN-17082), and the PACKAGED bifrost contract has no
    endpoint for every class, so routing the platform half through the real
    ladder would make this test depend on ambient contract bindings and on
    ``_load_bifrost_endpoints``' process cache. It did, briefly, and failed
    under xdist on the lab while passing locally. The class-independent half of
    the claim is asserted hermetically above.
    """
    task_type = "code_generation"
    overlay_decision = _overlay_decision(task_type=task_type, prompt=prompt)
    platform_decision = delta(
        _request(task_type=task_type, prompt=prompt, tenant_id=None),
        tenant_overlay=None,
        surface=EnumDelegationSurface.CUSTOMER_LOCAL,
    )

    assert overlay_decision.tier_name == TENANT_OVERLAY_TIER_NAME
    assert platform_decision.tier_name != TENANT_OVERLAY_TIER_NAME
    for field in _DOD_FIELDS:
        assert getattr(overlay_decision, field) == getattr(platform_decision, field), (
            f"{field} diverged between the overlay and platform routing sites"
        )


# --- Negative controls: green BEFORE and AFTER the fix ---------------------------


@pytest.mark.unit
def test_empty_response_on_the_overlay_route_still_fails() -> None:
    """The fix must not make the BYOK path permissive."""
    decision = _overlay_decision(task_type="summarization", prompt=_SUMMARY_PROMPT)
    result = handler_quality_gate.delta(_gate_input_from(decision, response=""))

    assert result.passed is False


@pytest.mark.unit
def test_refusal_response_on_the_overlay_route_still_fails() -> None:
    """A refusal is rejected on the overlay route on both sides of the fix."""
    decision = _overlay_decision(task_type="summarization", prompt=_SUMMARY_PROMPT)
    result = handler_quality_gate.delta(
        _gate_input_from(
            decision,
            response=(
                "I'm sorry, but I cannot help with that request. "
                + "As an AI language model I am unable to comply. " * 4
            ),
        )
    )

    assert result.passed is False


@pytest.mark.unit
def test_empty_bands_still_reach_the_legacy_fallback_unchanged() -> None:
    """The fallback is NARROWED by this fix, not deleted.

    Every task class the wire enum admits declares a ``definition_of_done``
    today, so no request can reach ``delta()`` and legitimately resolve empty
    bands — which is precisely why the overlay branch dropping them was
    invisible. The control is therefore made where the fallback lives: an input
    with both bands empty still takes ``_run_legacy_checks`` and still declines
    to claim adequacy authority it does not have (OMN-13370). This assertion is
    green on both sides of the fix; if it ever goes red, the fix reached the
    gate, which it must not.
    """
    result = handler_quality_gate.delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type="summarization",
            llm_response_content=_SUMMARY_RESPONSE,
            dod_deterministic=(),
            dod_heuristic=(),
        )
    )

    assert result.passed is False
    assert result.quality_score == pytest.approx(1.0)
    assert result.failure_reasons == (_NO_ADEQUACY_AUTHORITY_REASON,)
