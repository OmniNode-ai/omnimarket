# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17939: BYOK intake refuses a provider that is not on the customer catalogue.

OMN-17353 landed ``customer_provider_catalogue()`` as the single customer-facing
authority for which providers a customer may register a key for, and its own
docstring declares the intake contract:

    Intake surfaces (the onex-api ``POST /v1/tenants/me/inference-credentials``
    route) validate the submitted ``provider`` against it and refuse anything
    else with a typed error naming the allowed set.

Nothing wired that. ``ModelInferenceCredentialCreateRequest.provider`` was
constrained only for CHARSET safety (``^[A-Za-z0-9_-]+$``), because the value is
interpolated into ``api_key_ref`` and thence into an Infisical secret path
segment and a Kafka message key. That stops path traversal; it does not stop
membership drift. Any syntactically-safe token was accepted, so a customer could
register a key for a provider that can never route:

* a provider we declared NOT OFFERED (``gemini`` / ``glm`` / ``vertex``, each
  carrying a reason and the ticket that owns lifting it — OMN-17932),
* a provider with no delegation backend at all (``openai`` — OMN-17373),
* a bare typo (``openrooter``).

In each case the credential is stored, a secret path is minted and a
``credential-registered`` event is published for a route that will never exist.

The constraint belongs on the MODEL, not on the route handler: the model is what
onex-api imports (``docker/onex-api/routers/inference_credentials.py`` line 53),
so every caller — that route, a test, any future client — inherits the refusal
rather than each re-implementing it.

Fail-closed direction that this suite pins: a catalogue that cannot be LOADED is
a platform fault, not a client input fault. ``ByokCatalogError`` is a
``ValueError`` subclass, and pydantic converts a ``ValueError`` raised inside a
validator into a ``ValidationError`` — which the route would surface to the
customer as "your provider is invalid" (a 422) while the real cause is a broken
config on our side. The validator must therefore let that failure escape as a
non-``ValueError`` so the route's own handler maps it to a 503.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from omnimarket.projection import credential_publisher as publisher_mod
from omnimarket.projection.credential_publisher import (
    ModelInferenceCredentialCreateRequest,
    ProviderCatalogueUnavailableError,
)
from omnimarket.routing.byok_provider_backends import (
    ByokCatalogError,
    customer_provider_catalogue,
)

pytestmark = pytest.mark.unit


def _request(provider: str) -> ModelInferenceCredentialCreateRequest:
    """Build an intake body whose ONLY interesting field is ``provider``."""
    return ModelInferenceCredentialCreateRequest(
        name="my-key",
        provider=provider,
        key_value=SecretStr("not-a-real-key"),
    )


# --------------------------------------------------------------------------
# Positive control. Every refusal assertion below is meaningless if the
# catalogue is empty (a validator that refuses everything would pass them all),
# so prove the accepted set is non-empty and actually accepted FIRST.
# --------------------------------------------------------------------------


def test_the_catalogue_is_non_empty() -> None:
    """Positive control for every refusal test in this module."""
    assert customer_provider_catalogue(), (
        "the customer catalogue is empty; every refusal assertion in this "
        "module would pass vacuously"
    )


def test_every_catalogue_id_is_still_accepted() -> None:
    """The constraint must not pass by refusing everything."""
    for provider in customer_provider_catalogue():
        assert _request(provider).provider == provider


# --------------------------------------------------------------------------
# The defect: off-catalogue ids must be refused.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider",
    [
        "notaprovider",
        "openrooter",  # a plain typo of the one offered id
        "openai",  # no delegation backend at all (OMN-17373)
        "gemini",  # declared not_offered (OMN-17932)
        "glm",  # declared not_offered (OMN-17932)
        "vertex",  # declared not_offered (OMN-17932)
    ],
)
def test_an_off_catalogue_provider_is_refused(provider: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _request(provider)
    assert "provider" in str(excinfo.value)


@pytest.mark.parametrize("provider", ["claude", "anthropic", "claude-3-opus"])
def test_claude_is_refused_at_intake(provider: str) -> None:
    """Axiom 2: Claude is never a delegation target.

    The catalogue loader already refuses a Claude id in either list, so this
    cannot be reached through the catalogue. Asserted here anyway because it is
    a governing axiom and this is the customer-facing edge that enforces it.
    """
    with pytest.raises(ValidationError):
        _request(provider)


def test_the_refusal_names_the_offered_set() -> None:
    """A refusal a customer cannot act on is a bad refusal."""
    with pytest.raises(ValidationError) as excinfo:
        _request("notaprovider")
    message = str(excinfo.value)
    for offered in customer_provider_catalogue():
        assert offered in message, (
            f"the refusal does not name the offered provider {offered!r}, so a "
            "customer cannot tell what they were allowed to send"
        )


def test_the_refusal_does_not_leak_the_submitted_key() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _request("notaprovider")
    assert "not-a-real-key" not in str(excinfo.value)


# --------------------------------------------------------------------------
# The charset guarantee predates this ticket and must survive it: `provider`
# reaches an Infisical path segment and a Kafka message key.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider",
    ["open/router", "../openrouter", "open router", "openrouter\n", ""],
)
def test_the_charset_guarantee_is_preserved(provider: str) -> None:
    with pytest.raises(ValidationError):
        _request(provider)


# --------------------------------------------------------------------------
# Fail-closed: a broken catalogue is OUR fault, and must not be reported to the
# customer as though they sent a bad provider.
# --------------------------------------------------------------------------


def test_a_broken_catalogue_is_not_reported_as_a_client_input_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A catalogue that cannot load must not degrade to a 422-shaped refusal.

    ``ByokCatalogError`` subclasses ``ValueError``; pydantic swallows a
    ``ValueError`` raised in a validator and re-emits it as ``ValidationError``.
    If the validator lets that happen, a broken config on our side is surfaced
    to the customer as "your provider is invalid" and the real fault is hidden.
    """

    def _boom() -> tuple[str, ...]:
        raise ByokCatalogError("catalogue unreadable")

    monkeypatch.setattr(publisher_mod, "customer_provider_catalogue", _boom)

    # Asserting the specific type IS the guarantee: ProviderCatalogueUnavailableError
    # is deliberately not a ValueError, so if the validator ever let the raw
    # ByokCatalogError escape instead, pydantic would swallow it and re-emit a
    # ValidationError -- and this raises-check would fail rather than pass.
    with pytest.raises(ProviderCatalogueUnavailableError) as excinfo:
        _request("openrouter")

    assert not isinstance(excinfo.value, ValidationError), (
        "a broken catalogue was reported as a client input error; the intake "
        "validator must let the platform fault escape so the route maps it to "
        "a 503 rather than blaming the customer's input"
    )
    # The underlying cause is preserved for the operator reading the 503.
    assert isinstance(excinfo.value.__cause__, ByokCatalogError)
