# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex secret set llm.<provider>.api_key`` must make that key route (OMN-19205).

The CLI's own docstring and the resolver's remediation both tell a customer to
run ``onex secret set llm.glm.api_key``. On omnimarket 0.4.198-0.4.204 that
command stored the key and nothing routed to it: the row was filed under
provider ``llm.glm.api`` (``_provider_from_ref`` is built for minted refs), so
``substitute_local_byok_route`` found no customer key for ``glm`` and handed
back the house-shaped rung, which the customer-local terminus refuses. A
provider-only customer (no local model declared) saw the OMN-16200 "No local
model is declared" message instead, because that gate re-words exactly this
refusal. Measured on a clean install by the C29 producer (OMN-19200).

The route needs TWO things from the store, and a registration that supplies one
is not a working registration:

* the declared reference resolves, so tier selection picks the provider's rung
  (``backend_id_for_tier`` requires the rung's credential to resolve);
* a tenant-shaped reference is registered for the provider, so the BYOK
  substitution replaces the house-shaped rung with the customer's own.

Registering the same key under a hand-minted ``cred_localinstall_glm_<uuid>``
as well made the delegation answer on ``byok-glm`` -- proven 2026-09-22 on a
clean container. This file pins that ``onex secret set`` now does both.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_secret import secret_group
from omnimarket.inference.local_byok_credential_adapter import (
    LocalByokCredentialStore,
    resolve_local_byok_credential_ref,
)
from omnimarket.routing.customer_key_terminus import (
    EnumDelegationSurface,
    enforce_customer_key_terminus,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.routing.local_byok_route import substitute_local_byok_route
from omnimarket.tenant_credential_ref import is_tenant_credential_ref

pytestmark = pytest.mark.unit

_HOUSE_REF = "llm.glm.api_key"
_VALUE = "sk-customer-glm-key"
_TENANT = "3314bdfb-85c8-45fc-a601-e31e6b9d9d93"


@pytest.fixture(autouse=True)
def local_store_at_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )
    return db_path


def _run(args: list[str], stdin: str | None = None) -> object:
    return CliRunner().invoke(secret_group, args, input=stdin, catch_exceptions=False)


def _house_glm_rung() -> ModelResolvedDelegationBackend:
    """The shipped cheap_cloud GLM rung as tier selection resolves it."""
    return ModelResolvedDelegationBackend(
        backend_id="cloud-glm",
        model_id="glm-5.3-flash",
        endpoint_ref="https://api.z.ai/api/coding/paas/v4/chat/completions",
        tier="cheap_cloud",
        max_tokens=1024,
        timeout_ms=240000,
        secret_ref=_HOUSE_REF,
    )


def test_the_documented_registration_substitutes_the_customer_route() -> None:
    result = _run(["set", _HOUSE_REF], stdin=f"{_VALUE}\n")
    assert result.exit_code == 0, result.output

    routed = substitute_local_byok_route(_house_glm_rung())

    assert routed.backend_id == "byok-glm"
    assert routed.secret_ref is not None
    assert is_tenant_credential_ref(routed.secret_ref)
    assert (
        asyncio.run(LocalByokCredentialStore().get_secret(routed.secret_ref)) == _VALUE
    )


def test_the_substituted_route_clears_the_customer_local_terminus() -> None:
    _run(["set", _HOUSE_REF], stdin=f"{_VALUE}\n")
    routed = substitute_local_byok_route(_house_glm_rung())

    enforce_customer_key_terminus(
        tenant_id=_TENANT,
        task_type="document",
        correlation_id=__import__("uuid").uuid4(),
        surface=EnumDelegationSurface.CUSTOMER_LOCAL,
        api_key_ref=routed.secret_ref,
        api_key_env=routed.api_key_env,
        backend_ref=routed.backend_id,
        house_refs=frozenset({_HOUSE_REF}),
    )


def test_the_declared_reference_still_resolves_for_tier_selection() -> None:
    _run(["set", _HOUSE_REF], stdin=f"{_VALUE}\n")

    assert asyncio.run(LocalByokCredentialStore().get_secret(_HOUSE_REF)) == _VALUE


def test_the_output_names_the_route_reference_and_never_the_value() -> None:
    result = _run(["set", _HOUSE_REF], stdin=f"{_VALUE}\n")
    minted = resolve_local_byok_credential_ref("glm")

    assert minted is not None
    assert minted in result.output
    assert _VALUE not in result.output


def test_force_replaces_the_value_behind_both_references() -> None:
    _run(["set", _HOUSE_REF], stdin="first\n")
    result = _run(["set", _HOUSE_REF, "--force"], stdin="second\n")
    assert result.exit_code == 0, result.output

    minted = resolve_local_byok_credential_ref("glm")
    store = LocalByokCredentialStore()
    assert minted is not None
    assert asyncio.run(store.get_secret(minted)) == "second"
    assert asyncio.run(store.get_secret(_HOUSE_REF)) == "second"
    # One key per provider: the superseded route reference is gone.
    assert (
        len([r for r in asyncio.run(store.list_keys()) if r.startswith("cred_")]) == 1
    )


def test_deleting_the_declared_reference_withdraws_the_route_too() -> None:
    _run(["set", _HOUSE_REF], stdin=f"{_VALUE}\n")

    result = _run(["delete", _HOUSE_REF])

    assert result.exit_code == 0, result.output
    assert resolve_local_byok_credential_ref("glm") is None
    assert asyncio.run(LocalByokCredentialStore().list_keys()) == []


def test_a_provider_the_catalogue_does_not_offer_gets_no_route() -> None:
    """Gemini is ``not_offered`` for BYOK; storing its key mints no customer route."""
    result = _run(["set", "llm.gemini.api_key"], stdin=f"{_VALUE}\n")

    assert result.exit_code == 0, result.output
    assert resolve_local_byok_credential_ref("gemini") is None
    assert asyncio.run(LocalByokCredentialStore().list_keys()) == ["llm.gemini.api_key"]


def test_a_minted_reference_is_stored_as_given() -> None:
    ref = "cred_localinstall_glm_0123456789abcdef0123456789abcdef"

    _run(["set", ref], stdin=f"{_VALUE}\n")

    assert asyncio.run(LocalByokCredentialStore().list_keys()) == [ref]
