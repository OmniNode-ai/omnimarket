# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17434 — the bus-less local dispatch port enforces the customer-key terminus.

OMN-17082 landed the terminus (typed refusal, never a house credential on a
customer path) inside the routing authority's ``delta()``. The bus-less
``LocalDelegationDispatchPort`` never calls ``delta()``: it resolves backends
through ``resolve_delegation_backend`` (initial pin / tier-derived initial /
every escalation hop), so a customer-attributed delegation on that port could
bind a house ``secret_ref`` and hand it to the effect with no typed refusal.

These tests reuse the OMN-17082 fixtures (a two-backend contract: one free
local backend, one house-credentialed cloud backend; every house credential
planted) so a route that reaches the effect is a route that WOULD have
executed on OmniNode's provider account.

The four proofs:

1. customer tenant + explicit platform ``backend_id`` → ``CustomerKeyRefusedError``
   naming the credential, and the effect is never called;
2. keyless customer with no pin still reaches the free local rung (the honest
   CUSTOMER_LOCAL terminus — OMN-17082 item (c));
3. house / untenanted work on the same port is unchanged;
4. an escalation hop from the free local rung to a house-credentialed tier is
   refused for a customer tenant.

Falsification: removing the guard call in ``dispatch()`` fails (1) and (4)
with the effect having received ``llm.glm.api_key``.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG
from omnimarket.routing.customer_key_terminus import (
    CUSTOMER_PROVIDER_KEY_ABSENT_ERROR_CODE,
    CustomerKeyRefusedError,
    EnumCustomerKeyRefusalReason,
    EnumDelegationSurface,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from tests.test_omn17082_customer_key_terminus import (  # noqa: F401 — fixtures
    house_keys_planted,
    local_and_house_backends,
)

_CUSTOMER = "acme-corp"
_HOUSE_SECRET_REF = "llm.glm.api_key"
_HOUSE_API_KEY_ENV = "LLM_GLM_API_KEY"

# A gate-passing artifact for the ``research`` class (same text the OMN-15156
# port tests use), so the proofs stay about the credential, not the gate.
_GOOD_RESEARCH = (
    "According to Smith (2020) and the theorem in section 3, the tradeoff is "
    "significant because the evidence shows X; therefore we conclude Y. See "
    "references [12] for the methodical analysis and the risk profile."
)

# The two backends the OMN-17082 fixture contract declares, in the shape the
# bus-less port resolves them to. ``cloud-glm`` carries BOTH credential fields
# (``api_key_env`` is a genuine additional fallback the effect boundary
# resolves — OMN-13943); ``local-coder`` carries neither.
_LOCAL_BACKEND = ModelResolvedDelegationBackend(
    backend_id="local-coder",
    model_id="qwen-coder",
    endpoint_ref="http://local.test:8000/v1/chat/completions",
    tier="local",
    max_tokens=8192,
    timeout_ms=30000,
)
_HOUSE_BACKEND = ModelResolvedDelegationBackend(
    backend_id="cloud-glm",
    model_id="glm-5.3-flash",
    endpoint_ref="https://api.z.ai/api/coding/paas/v4/chat/completions",
    tier="cheap_cloud",
    max_tokens=65536,
    timeout_ms=30000,
    secret_ref=_HOUSE_SECRET_REF,
    api_key_env=_HOUSE_API_KEY_ENV,
)
_FIXTURE_BACKENDS = {
    _LOCAL_BACKEND.backend_id: _LOCAL_BACKEND,
    _HOUSE_BACKEND.backend_id: _HOUSE_BACKEND,
}


class _RecordingEffect:
    """Effect handler that records every request it is handed.

    ``outcomes`` is consumed per call: a failure class makes that call a
    transport failure of that class; ``None`` (or an exhausted script) succeeds
    with a gate-passing artifact.
    """

    def __init__(
        self, outcomes: list[EnumDelegationFailureClass | None] | None = None
    ) -> None:
        self.calls: list[ModelLlmDelegationCallRequest] = []
        self._outcomes = list(outcomes or [])

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request)
        failure_class = self._outcomes.pop(0) if self._outcomes else None
        if failure_class is not None:
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=False,
                failure_class=failure_class,
                error_message=f"simulated {failure_class.value}",
                tokens_in=0,
                tokens_out=0,
                latency_ms=1,
                actual_cost_usd=Decimal("0"),
                savings_usd=Decimal("0"),
            )
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=_GOOD_RESEARCH,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )


@pytest.fixture
def fixture_backends_resolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the port resolve the two fixture backends by id, and the ladder walk
    local -> cheap_cloud on escalation.

    Resolution is stubbed at the port's own seams (the same seams the
    OMN-15156 / OMN-14001 port tests use) so the proofs exercise ``dispatch()``'s
    executable-backend point, not the bifrost file loader. The house-credential
    SET is NOT stubbed: it is derived from the fixture contract through the
    routing authority, exactly as production derives it.
    """

    def _resolve(
        task_type: str, *, backend_id: str | None = None, **_: object
    ) -> ModelResolvedDelegationBackend:
        if backend_id is None:
            return _LOCAL_BACKEND
        try:
            return _FIXTURE_BACKENDS[backend_id]
        except KeyError as exc:
            raise RuntimeError(f"No delegation backend {backend_id!r}") from exc

    monkeypatch.setattr(port_mod, "resolve_delegation_backend", _resolve)
    monkeypatch.setattr(port_mod, "first_eligible_tier", lambda *_a, **_k: "local")
    monkeypatch.setattr(
        port_mod,
        "backend_id_for_tier",
        lambda tier, _task_type: {"local": "local-coder", "cheap_cloud": "cloud-glm"}[
            tier
        ],
    )
    monkeypatch.setattr(
        port_mod,
        "next_eligible_tier",
        lambda current_tier, *_a, **_k: (
            "cheap_cloud" if current_tier == "local" else None
        ),
    )
    monkeypatch.setattr(port_mod, "tier_for_backend", lambda _backend_id: None)


def _port(tmp_path: Path, effect: _RecordingEffect) -> LocalDelegationDispatchPort:
    return LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "evidence.sqlite",
        effect_process_boundary=False,
    )


def _dispatch(
    port: LocalDelegationDispatchPort,
    *,
    tenant_id: str | None,
    backend_id: str | None = None,
    correlation_id: UUID | None = None,
) -> dict[str, object]:
    return asyncio.run(
        port.dispatch(
            prompt="explain the tradeoff",
            task_type="research",
            correlation_id=correlation_id or uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=tenant_id,
            backend_id=backend_id,
        )
    )


def _executed_on_house_credential(effect: _RecordingEffect) -> str:
    """Failure text naming the credential(s) a customer request executed on."""
    executed = [
        (call.model_id, call.secret_ref, call.api_key_env) for call in effect.calls
    ]
    return (
        "house-credential fallthrough on the bus-less customer path: the effect "
        "was handed a customer-attributed request with (model, secret_ref, "
        f"api_key_env) = {executed!r} instead of a typed refusal"
    )


# --- (1) customer + explicit platform backend pin -> typed refusal ------------


@pytest.mark.usefixtures(
    "local_and_house_backends", "house_keys_planted", "fixture_backends_resolvable"
)
def test_customer_pinning_a_house_backend_is_refused_before_the_effect_runs(
    tmp_path: Path,
) -> None:
    """The refusal names the credential and the effect never sees the request."""
    effect = _RecordingEffect()
    correlation_id = uuid4()

    try:
        _dispatch(
            _port(tmp_path, effect),
            tenant_id=_CUSTOMER,
            backend_id="cloud-glm",
            correlation_id=correlation_id,
        )
    except CustomerKeyRefusedError as exc:
        refusal = exc.refusal
    else:
        pytest.fail(_executed_on_house_credential(effect))

    assert refusal.error_code == CUSTOMER_PROVIDER_KEY_ABSENT_ERROR_CODE
    assert (
        refusal.reason is EnumCustomerKeyRefusalReason.HOUSE_CREDENTIAL_ON_CUSTOMER_PATH
    )
    assert refusal.surface is EnumDelegationSurface.CUSTOMER_LOCAL
    assert refusal.tenant_id == _CUSTOMER
    assert refusal.task_type == "research"
    assert refusal.correlation_id == correlation_id
    assert refusal.attempted_api_key_ref == _HOUSE_SECRET_REF
    assert refusal.attempted_api_key_env == _HOUSE_API_KEY_ENV
    assert refusal.attempted_backend_ref == "cloud-glm"
    # Refs are safe to surface; the planted VALUE must never be.
    assert "house-key-planted-by-test" not in str(refusal.model_dump())
    assert effect.calls == [], _executed_on_house_credential(effect)


# --- (2) keyless customer, no pin -> the free local rung stays reachable -----


@pytest.mark.usefixtures(
    "local_and_house_backends", "house_keys_planted", "fixture_backends_resolvable"
)
def test_keyless_customer_still_reaches_the_free_local_rung(tmp_path: Path) -> None:
    """The uncredentialed local backend IS the honest CUSTOMER_LOCAL terminus.

    Nothing of OmniNode's is involved on the customer's own machine, so the
    guard must let this through — refusing it would break the sanctioned
    customer-local CLI (OMN-17082 item (c)).
    """
    effect = _RecordingEffect()

    result = _dispatch(_port(tmp_path, effect), tenant_id=_CUSTOMER)

    assert result["status"] == "completed"
    assert result["model_name"] == "qwen-coder"
    assert len(effect.calls) == 1
    assert effect.calls[0].secret_ref is None
    assert effect.calls[0].api_key_env is None


# --- (3) house / untenanted work on the same port is unchanged ---------------


@pytest.mark.usefixtures(
    "local_and_house_backends", "house_keys_planted", "fixture_backends_resolvable"
)
@pytest.mark.parametrize("tenant_id", [HOUSE_TENANT_SLUG, None, "   "])
def test_platform_work_pinning_a_house_backend_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tenant_id: str | None
) -> None:
    """OmniNode's own work on OmniNode's own key is not pooling."""
    # The untenanted path must not be re-attributed by a stray env interim.
    monkeypatch.delenv("ONEX_TENANT_ID", raising=False)
    effect = _RecordingEffect()

    result = _dispatch(
        _port(tmp_path, effect), tenant_id=tenant_id, backend_id="cloud-glm"
    )

    assert result["status"] == "completed"
    assert result["model_name"] == "glm-5.3-flash"
    assert len(effect.calls) == 1
    assert effect.calls[0].secret_ref == _HOUSE_SECRET_REF


# --- (4) escalation hop onto a house-credentialed tier is refused ------------


@pytest.mark.usefixtures(
    "local_and_house_backends", "house_keys_planted", "fixture_backends_resolvable"
)
def test_customer_escalation_onto_a_house_tier_is_refused(tmp_path: Path) -> None:
    """A retryable transport failure on the free rung escalates local ->
    cheap_cloud; for a customer that hop must terminate in the typed refusal,
    never in an effect call carrying the house credential.
    """
    effect = _RecordingEffect(outcomes=[EnumDelegationFailureClass.RATE_LIMITED])

    try:
        _dispatch(_port(tmp_path, effect), tenant_id=_CUSTOMER)
    except CustomerKeyRefusedError as exc:
        refusal = exc.refusal
    else:
        pytest.fail(_executed_on_house_credential(effect))

    assert (
        refusal.reason is EnumCustomerKeyRefusalReason.HOUSE_CREDENTIAL_ON_CUSTOMER_PATH
    )
    assert refusal.surface is EnumDelegationSurface.CUSTOMER_LOCAL
    assert refusal.attempted_api_key_ref == _HOUSE_SECRET_REF
    assert refusal.attempted_backend_ref == "cloud-glm"
    # Exactly the free local attempt ran; the house hop was refused, not executed.
    assert [call.secret_ref for call in effect.calls] == [None]


@pytest.mark.usefixtures(
    "local_and_house_backends", "house_keys_planted", "fixture_backends_resolvable"
)
def test_platform_escalation_onto_a_house_tier_still_executes(tmp_path: Path) -> None:
    """Control for (4): the same hop for house work proceeds onto the house key."""
    effect = _RecordingEffect(outcomes=[EnumDelegationFailureClass.RATE_LIMITED])

    result = _dispatch(_port(tmp_path, effect), tenant_id=HOUSE_TENANT_SLUG)

    assert result["status"] == "completed"
    assert [call.secret_ref for call in effect.calls] == [None, _HOUSE_SECRET_REF]
