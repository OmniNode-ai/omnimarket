# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A keyless customer on their own machine is told a LOCAL fix (OMN-19205 AC2).

Until this change every customer-key refusal, on every surface, carried one
remediation: register a key by POSTing to the cloud intake endpoint. A customer
delegating from their own machine has no account there and needs none. The two
things that fix their refusal are local: register their own provider key with
``onex secret set llm.<provider>.api_key``, or declare a local model in
``~/.omninode/delegation/bifrost_overrides.yaml``.

Two texts reach that customer, and they obey different rules:

* the TYPED refusal (``ModelCustomerKeyRefusal.remediation`` and ``.message``)
  is data -- it may name the command exactly;
* the BOUNDARY line (the exception message, and the OMN-16200 "no local model"
  refusal a provider-only customer hits first) may cross the consume
  boundary, where ``sanitize_error_message`` collapses any text carrying
  ``secret``, ``api_key`` or ``credential``. So those lines name the local fix
  in words and are proven to survive the real sanitizer.

The cloud surface is out of scope here (M4.5) and is pinned UNCHANGED below.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from omnibase_infra.utils.util_error_sanitization import sanitize_error_message

from omnimarket.routing.customer_key_terminus import (
    CUSTOMER_KEY_REMEDIATION,
    CustomerKeyRefusedError,
    EnumCustomerKeyRefusalReason,
    EnumDelegationSurface,
    ModelCustomerKeyRefusal,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
    refuse_undeclared_local_model,
)

pytestmark = pytest.mark.unit

_CLOUD_ENDPOINT = "/v1/tenants/me/inference-credentials"
_TENANT = "3314bdfb-85c8-45fc-a601-e31e6b9d9d93"


def _refusal(
    surface: EnumDelegationSurface, reason: EnumCustomerKeyRefusalReason
) -> ModelCustomerKeyRefusal:
    return ModelCustomerKeyRefusal(
        reason=reason,
        tenant_id=_TENANT,
        task_type="document",
        surface=surface,
        correlation_id=uuid4(),
    )


@pytest.mark.parametrize("reason", list(EnumCustomerKeyRefusalReason))
def test_the_typed_customer_local_refusal_names_the_local_commands(
    reason: EnumCustomerKeyRefusalReason,
) -> None:
    refusal = _refusal(EnumDelegationSurface.CUSTOMER_LOCAL, reason)

    for text in (refusal.remediation, refusal.message):
        assert "onex secret set llm.<provider>.api_key" in text
        assert "bifrost_overrides.yaml" in text
        assert _CLOUD_ENDPOINT not in text


@pytest.mark.parametrize("reason", list(EnumCustomerKeyRefusalReason))
def test_the_customer_local_boundary_line_is_local_and_survives_the_sanitizer(
    reason: EnumCustomerKeyRefusalReason,
) -> None:
    exc = CustomerKeyRefusedError(
        _refusal(EnumDelegationSurface.CUSTOMER_LOCAL, reason)
    )

    sanitized = sanitize_error_message(exc)

    assert "[REDACTED" not in sanitized
    assert "on this machine" in sanitized
    assert "local model" in sanitized
    assert _CLOUD_ENDPOINT not in sanitized
    assert "Register a provider key for this tenant" not in sanitized


def test_for_missing_key_on_customer_local_carries_the_local_remedy() -> None:
    exc = CustomerKeyRefusedError.for_missing_key(
        tenant_id=_TENANT,
        task_type="document",
        correlation_id=uuid4(),
        surface=EnumDelegationSurface.CUSTOMER_LOCAL,
    )

    assert "onex secret set" in exc.refusal.remediation
    assert _CLOUD_ENDPOINT not in str(exc)


@pytest.mark.parametrize("reason", list(EnumCustomerKeyRefusalReason))
def test_the_cloud_surface_is_unchanged(reason: EnumCustomerKeyRefusalReason) -> None:
    """M4.5 owns cloud wording; this change must not move it."""
    refusal = _refusal(EnumDelegationSurface.CLOUD, reason)

    assert refusal.remediation == CUSTOMER_KEY_REMEDIATION
    assert "Register a provider key for this tenant and retry." in (
        refusal.boundary_message
    )


def test_an_explicit_remediation_is_kept_on_either_surface() -> None:
    refusal = ModelCustomerKeyRefusal(
        reason=EnumCustomerKeyRefusalReason.NO_PROVIDER_KEY_REGISTERED,
        tenant_id=_TENANT,
        task_type="document",
        surface=EnumDelegationSurface.CUSTOMER_LOCAL,
        correlation_id=uuid4(),
        remediation="caller-chosen text",
    )

    assert refusal.remediation == "caller-chosen text"


def test_the_no_local_model_refusal_also_offers_a_provider_key() -> None:
    """The provider-only customer's first refusal (OMN-16200 gate) names both fixes."""
    house_rung = ModelResolvedDelegationBackend(
        backend_id="cloud-glm",
        model_id="glm-5.3-flash",
        endpoint_ref="https://api.z.ai/api/coding/paas/v4/chat/completions",
        tier="cheap_cloud",
        max_tokens=1024,
        timeout_ms=240000,
        secret_ref="llm.glm.api_key",
    )
    with pytest.raises(Exception) as exc_info:  # noqa: PT011 -- the type is pinned by OMN-16200's own test
        refuse_undeclared_local_model(
            tenant_id=_TENANT,
            backend=house_rung,
            house_refs=frozenset({"llm.glm.api_key"}),
            backends=[
                {"backend_id": "local-coder", "tier": "local", "endpoint_url": None},
                {"backend_id": "cloud-glm", "tier": "cheap_cloud"},
            ],
        )

    sanitized = sanitize_error_message(exc_info.value)
    assert "[REDACTED" not in sanitized
    assert "No local model is declared" in sanitized
    assert "register your own provider key on this machine" in sanitized
    assert _CLOUD_ENDPOINT not in sanitized
    assert "truncated" not in sanitized
