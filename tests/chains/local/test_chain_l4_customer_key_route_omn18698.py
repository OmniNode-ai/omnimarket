# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""L4 chain pair: OpenRouter on the CUSTOMER's key, with no gateway (OMN-18698).

Row L4 of the local-path MVP (`beta/GOAL.md`, ticket OMN-18694): *the skill
dispatches to OpenRouter on the customer's key, no gateway*.

The pair
--------
``test_golden_chain_...``
    A customer registers their own OpenRouter key in the local store. The
    ladder resolves the HOUSE-keyed OpenRouter rung, the BYOK substitution
    replaces it with the declared customer route, the effect boundary presents
    the customer's value, and the terminal receipt names the customer's
    backend, the catalogue's model, the rung's tier and a cost of zero.

``test_error_chain_...``
    The same chain with nothing registered -- the defect-shaped condition. All
    the rung has is the HOUSE reference, and the chain must terminate in the
    typed OMN-17082 customer-key refusal naming the code, the dotted code and
    the surface. It must NOT reach the provider, and it must NOT climb the
    ladder looking for another rung to answer on.

Why the error chain is the load-bearing half
--------------------------------------------
A golden chain alone would still pass in a world where the house credential
answered customer work -- it would simply be OUR key paying for their call,
which is exactly what OMN-17082 forbids and what a green test would hide. The
error chain is what makes the absence of a house answer observable: the
provider records every request it receives, and the assertion is that it
received none.

Falsifier (OMN-18698 AC2): reverting ``substitute_local_byok_route`` to a
passthrough turns the golden chain red at the backend-id assertion; removing
the customer-key terminus turns the error chain red at the refusal-code
assertion, and if an ambient house key were also present it would turn red at
"no request reached the provider" as well.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.inference.local_byok_credential_adapter import (
    register_local_byok_credential,
)
from omnimarket.routing.customer_key_terminus import (
    CUSTOMER_PROVIDER_KEY_ABSENT_ERROR_CODE,
    CUSTOMER_PROVIDER_KEY_ABSENT_ONEX_CODE,
    EnumDelegationSurface,
)
from tests.chains.local.harness import (
    BYOK_BACKEND_ID,
    HOUSE_BACKEND_ID,
    PROVIDER_SLUG,
    LocalProviderStub,
    house_openrouter_rung,
    install_rungs,
    local_byok_catalogue,
    no_ambient_provider_credentials,
    run_local_delegation,
    shipped_byok_model_name,
    use_local_store,
)

pytestmark = [pytest.mark.unit, pytest.mark.local_chain]

# A synthetic value, generated here and never read from anywhere. It stands in
# for the customer's real key so the pair can prove WHICH credential the
# provider was handed.
_CUSTOMER_KEY_VALUE = "sk-or-omn18698-customer-supplied-value"


async def test_golden_chain_customer_key_routes_and_pays(
    provider_stub: LocalProviderStub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A registered customer key answers the delegation, on the customer's route."""
    with no_ambient_provider_credentials(monkeypatch):
        db_path = use_local_store(monkeypatch, tmp_path)
        local_byok_catalogue(monkeypatch, tmp_path, provider_stub.completions_url)
        house_openrouter_rung(monkeypatch)

        customer_ref = register_local_byok_credential(
            PROVIDER_SLUG, _CUSTOMER_KEY_VALUE, db_path=db_path
        )
        correlation_id = uuid4()

        response = await run_local_delegation(
            prompt="write a function that parses a semver string",
            db_path=db_path,
            correlation_id=correlation_id,
        )

    # -- the terminal ------------------------------------------------------
    assert response.status == "completed", response.error_message
    assert response.correlation_id == correlation_id
    assert response.response.strip() == "print('ok')"

    # -- the receipt names the CUSTOMER's route, not the house rung --------
    assert response.provider == provider_stub.completions_url
    assert response.model_name == shipped_byok_model_name()
    assert len(response.attempts) == 1, (
        "the customer's chain of responders has exactly one member; a second "
        "attempt means the ladder climbed onto a house rung"
    )
    attempt = response.attempts[0]
    assert attempt.backend_id == BYOK_BACKEND_ID
    assert attempt.backend_id != HOUSE_BACKEND_ID
    assert attempt.tier == "cheap_frontier", (
        "substitution replaces the rung, not its place in the ladder"
    )

    # -- the platform spent nothing ---------------------------------------
    assert response.metrics.cost_usd == 0.0
    assert attempt.cost_usd == 0.0

    # -- the provider was handed the CUSTOMER's value, by reference --------
    assert customer_ref.startswith("cred_"), (
        "the substitution must carry a tenant-shaped minted reference"
    )
    assert len(provider_stub.authorizations) == 1
    assert provider_stub.presented_credential == _CUSTOMER_KEY_VALUE


async def test_error_chain_no_registered_key_refuses_rather_than_using_the_house_key(
    provider_stub: LocalProviderStub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With nothing registered the chain refuses; no house credential answers."""
    with no_ambient_provider_credentials(monkeypatch):
        db_path = use_local_store(monkeypatch, tmp_path)
        local_byok_catalogue(monkeypatch, tmp_path, provider_stub.completions_url)
        rung = house_openrouter_rung(monkeypatch)
        # The defect-shaped condition: a house-keyed rung, and no customer key.
        # Point the house rung at the live stub too, so "the provider was never
        # called" is a fact about the CREDENTIAL and not about an unreachable
        # host -- without this the assertion would pass for the wrong reason.
        rung["endpoint_url"] = provider_stub.completions_url
        install_rungs(monkeypatch, [rung])

        correlation_id = uuid4()
        response = await run_local_delegation(
            prompt="write a function that parses a semver string",
            db_path=db_path,
            correlation_id=correlation_id,
        )

    # -- a typed refusal, not a generic failure ----------------------------
    assert response.status == "failed"
    assert response.correlation_id == correlation_id
    assert CUSTOMER_PROVIDER_KEY_ABSENT_ONEX_CODE in response.error_message
    assert CUSTOMER_PROVIDER_KEY_ABSENT_ERROR_CODE in response.error_message, (
        "customer surfaces route on the dotted code; it must survive onto the "
        "terminal the caller reads, not only into a log line"
    )
    assert EnumDelegationSurface.CUSTOMER_LOCAL.value in response.error_message, (
        "the refusal must name the surface -- running on the customer's own "
        "laptop does not make OmniNode's provider account theirs"
    )

    # -- the house credential did not answer -------------------------------
    assert provider_stub.authorizations == [], (
        "no request may reach the provider at all: there was no customer "
        "credential to present, and the house one must never be presented "
        "on a customer's behalf (OMN-17082)"
    )

    # -- refused at the ROUTING terminus, before any attempt ---------------
    # Which of the two refusals fires is a fact worth pinning. This one is
    # raised by ``refuse_house_credentialed_route`` before a backend is ever
    # attempted, so there is no attempt record and no effect-boundary
    # ``credential_refusal``; the effect-boundary refusal is L6's pair.
    assert response.attempts == [], (
        "the ladder must not climb looking for another rung to answer on"
    )
    assert response.credential_refusal is None
