# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""L5 chain pair: the key resolves from the local store, never env (OMN-18698).

Row L5 of the local-path MVP (`beta/GOAL.md`, ticket OMN-18695): *the
customer's own provider key resolves from the local SQLite store, never env*.

The pair
--------
``test_golden_chain_...``
    The store holds the customer's value. The process environment holds a
    DIFFERENT value under every name that could plausibly answer the same
    reference: the reference's own literal spelling, its dotted-to-uppercase
    convention form, and the provider-native ``OPENROUTER_API_KEY``. The
    provider must be handed the STORE's value.

``test_error_chain_...``
    The store's row survives but its value is gone, and the same three
    environment variables are present -- the defect-shaped condition, and
    exactly the state in which the pre-OMN-18694 resolver answered a tenant
    reference out of the environment. The chain must refuse, typed, naming
    the customer's own reference, and must never reach the provider.

Why a decoy value rather than an empty environment
--------------------------------------------------
An assertion that the store's value arrived proves nothing if the environment
is empty: the same test passes against a resolver that reads env first, when
env has nothing to say. The decoy is what makes the two orders
distinguishable. The values here are synthetic strings minted in this file
and read from nowhere.

Falsifier (OMN-18698 AC2): deleting the ``is_tenant_credential_ref`` early
return from ``_ConventionFallbackSecretStore.get_secret`` turns the golden
chain red (the decoy value arrives at the provider) and the error chain red
(the delegation succeeds on an environment value instead of refusing).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.inference.local_byok_credential_adapter import (
    LOCAL_CREDENTIAL_TABLE,
    register_local_byok_credential,
)
from omnimarket.models.delegation.local_credential_refusal import (
    EnumLocalCredentialRefusalReason,
)
from omnimarket.tenant_credential_ref import is_tenant_credential_ref
from tests.chains.local.harness import (
    BYOK_BACKEND_ID,
    PROVIDER_SLUG,
    LocalProviderStub,
    house_openrouter_rung,
    local_byok_catalogue,
    no_ambient_provider_credentials,
    run_local_delegation,
    use_local_store,
)

pytestmark = [pytest.mark.unit, pytest.mark.local_chain]

#: What the customer put in their local store.
_STORE_VALUE = "sk-or-omn18698-value-from-the-local-store"

#: What a stray environment holds. If this reaches the provider, the store was
#: not the source of truth.
_DECOY_ENV_VALUE = "sk-or-omn18698-value-from-the-environment"


def _plant_decoy_environment(
    monkeypatch: pytest.MonkeyPatch, customer_ref: str
) -> None:
    """Set every environment name that could answer ``customer_ref``.

    Three names, because the resolver composes three lookups: the literal
    reference, the dotted-ref-to-uppercase convention, and the provider-native
    alias. All three read the process environment, and on a customer machine
    the environment is exactly where a house key lives.
    """
    monkeypatch.setenv(customer_ref, _DECOY_ENV_VALUE)
    monkeypatch.setenv(customer_ref.upper().replace(".", "_"), _DECOY_ENV_VALUE)
    monkeypatch.setenv("OPENROUTER_API_KEY", _DECOY_ENV_VALUE)


def _empty_the_stored_value(db_path: Path, customer_ref: str) -> None:
    """Leave the reference registered and remove the value behind it.

    A real condition -- a truncated or partially restored store -- and the
    precise shape the resolver used to paper over: the reference still
    resolves to a route, so routing proceeds, and only the VALUE lookup comes
    back empty.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        updated = conn.execute(
            f"UPDATE {LOCAL_CREDENTIAL_TABLE} SET secret_value = '' "
            "WHERE secret_ref = ?",
            (customer_ref,),
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    assert updated == 1, "the fixture must empty exactly the registered row"


async def test_golden_chain_store_value_wins_over_every_environment_name(
    provider_stub: LocalProviderStub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The provider is handed the store's value while env offers another."""
    with no_ambient_provider_credentials(monkeypatch):
        db_path = use_local_store(monkeypatch, tmp_path)
        local_byok_catalogue(monkeypatch, tmp_path, provider_stub.completions_url)
        house_openrouter_rung(monkeypatch)

        customer_ref = register_local_byok_credential(
            PROVIDER_SLUG, _STORE_VALUE, db_path=db_path
        )
        assert is_tenant_credential_ref(customer_ref), (
            "the store must mint a tenant-shaped reference; the env exemption "
            "is keyed on that shape"
        )
        _plant_decoy_environment(monkeypatch, customer_ref)

        response = await run_local_delegation(
            prompt="write a function that parses a semver string",
            db_path=db_path,
            correlation_id=uuid4(),
        )

    assert response.status == "completed", response.error_message
    assert response.attempts[0].backend_id == BYOK_BACKEND_ID

    # The whole row, in one assertion: the STORE answered, not the process
    # environment, with both offering a value for the same reference.
    assert provider_stub.presented_credential == _STORE_VALUE
    assert provider_stub.presented_credential != _DECOY_ENV_VALUE


async def test_error_chain_an_empty_store_refuses_rather_than_reading_env(
    provider_stub: LocalProviderStub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With no stored value the chain refuses; the environment does not answer."""
    with no_ambient_provider_credentials(monkeypatch):
        db_path = use_local_store(monkeypatch, tmp_path)
        local_byok_catalogue(monkeypatch, tmp_path, provider_stub.completions_url)
        house_openrouter_rung(monkeypatch)

        customer_ref = register_local_byok_credential(
            PROVIDER_SLUG, _STORE_VALUE, db_path=db_path
        )
        _empty_the_stored_value(db_path, customer_ref)
        _plant_decoy_environment(monkeypatch, customer_ref)

        correlation_id = uuid4()
        response = await run_local_delegation(
            prompt="write a function that parses a semver string",
            db_path=db_path,
            correlation_id=correlation_id,
        )

    # -- refused, and refused about the CUSTOMER's own reference -----------
    assert response.status == "failed"
    assert response.correlation_id == correlation_id
    refusal = response.credential_refusal
    assert refusal is not None, (
        "an unresolvable customer reference must produce a typed refusal, not "
        "a generic terminal"
    )
    assert refusal.reason is EnumLocalCredentialRefusalReason.CREDENTIAL_ABSENT
    assert refusal.credential_ref == customer_ref
    assert refusal.credential_env is None, (
        "a tenant reference declares no environment fallback, and the refusal "
        "must not invite the customer to create one"
    )
    assert refusal.retryable is False

    # -- the environment did not answer ------------------------------------
    assert provider_stub.authorizations == [], (
        "three environment variables held a usable-looking value for this "
        "reference; none of them may be consulted for a tenant-shaped ref"
    )
    assert _DECOY_ENV_VALUE not in refusal.message, (
        "a refusal names references, never values"
    )
