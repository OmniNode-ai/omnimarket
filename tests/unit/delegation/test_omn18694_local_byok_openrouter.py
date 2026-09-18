# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18694 gap A: a customer's own OpenRouter key routes a local delegation.

Each test names the acceptance criterion it holds and, where the criterion
declares one, reproduces its FALSIFIER rather than asserting the happy path.
The falsifiers matter more than the assertions here: three of the four ACs are
stated as "this must not work", and a test that only proves the good path would
pass identically against the pre-change code.

Measured RED on 2026-09-18, before this change, on a machine with a house
``OPENROUTER_API_KEY`` present in the environment (length 73):

  * ``resolve_api_key("llm.openrouter.api_key")`` resolved that house value
    from the local default store -- so the house credential WAS reachable from
    the local path (AC3 falsifier fired);
  * a customer-shaped ``cred_..._openrouter_<32hex>`` ref resolved to nothing,
    because there was nowhere on the machine to put a customer key (AC1 was
    unreachable);
  * that same customer-shaped ref WAS answered by an environment variable
    spelled with the ref's own name (AC4 falsifier fired).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnimarket.inference.local_byok_credential_adapter import (
    LOCAL_INSTALL_TENANT_ID,
    LocalByokCredentialError,
    LocalByokCredentialStore,
    register_local_byok_credential,
    registered_local_byok_providers,
    resolve_local_byok_credential_ref,
    revoke_local_byok_credential,
)
from omnimarket.inference.secret_store_resolver import (
    SecretResolutionError,
    resolve_api_key,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.routing.local_byok_route import (
    house_provider_slug,
    substitute_local_byok_route,
)
from omnimarket.tenant_credential_ref import is_tenant_credential_ref

pytestmark = pytest.mark.unit

#: A value that is never a real key. Length only ever asserted, never printed.
_FAKE_CUSTOMER_KEY = "sk-or-v1-" + "c" * 40
_FAKE_HOUSE_KEY = "sk-or-v1-" + "h" * 40


@pytest.fixture
def local_db(tmp_path: Path) -> Path:
    """A local delegation database of this test's own, never the real one."""
    return tmp_path / "delegation.sqlite"


def _house_rung(
    *, secret_ref: str | None = "llm.openrouter.api_key"
) -> ModelResolvedDelegationBackend:
    """A resolved platform rung shaped like the ones bifrost actually yields."""
    return ModelResolvedDelegationBackend(
        backend_id="openrouter-qwen3-coder-480b",
        model_id="house/model:free",
        endpoint_ref="https://openrouter.ai/api/v1/chat/completions",
        tier="cheap_cloud",
        max_tokens=4096,
        timeout_ms=30000,
        secret_ref=secret_ref,
        api_key_env="OPENROUTER_API_KEY",
    )


class TestAc4NoEnvironmentVariableIsConsulted:
    """AC4: the customer key is read by reference from the local store and never
    from an environment variable.

    Falsifier: setting the provider's conventional environment variable makes an
    otherwise-failing run succeed.
    """

    def test_conventional_provider_env_var_does_not_answer_a_customer_ref(
        self, monkeypatch: pytest.MonkeyPatch, local_db: Path
    ) -> None:
        """The exact AC4 falsifier: OPENROUTER_API_KEY set, no store row, must fail."""
        monkeypatch.setenv("OPENROUTER_API_KEY", _FAKE_HOUSE_KEY)
        monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
        ref = f"cred_{LOCAL_INSTALL_TENANT_ID}_openrouter_{'a' * 32}"

        with pytest.raises(SecretResolutionError) as excinfo:
            resolve_api_key(ref, required=True)

        # The refusal names the reference, never a value.
        assert ref in str(excinfo.value)
        assert _FAKE_HOUSE_KEY not in str(excinfo.value)

    def test_env_var_named_exactly_like_the_ref_does_not_answer_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The RED-3 hole: a literal env var spelled with the ref's own name.

        This is the lookup ``AdapterEnvSecretStore`` performs. Before this
        change it answered a tenant ref; the store is now not consulted at all
        for that ref shape, so it must not.
        """
        monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
        ref = f"cred_{LOCAL_INSTALL_TENANT_ID}_openrouter_{'b' * 32}"
        monkeypatch.setenv(ref, _FAKE_HOUSE_KEY)

        assert resolve_api_key(ref, required=False) is None

    def test_uppercased_convention_form_does_not_answer_a_customer_ref(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dotted-ref -> ENV_VAR convention lookup is skipped too."""
        monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
        ref = f"cred_{LOCAL_INSTALL_TENANT_ID}_openrouter_{'c' * 32}"
        monkeypatch.setenv(ref.upper(), _FAKE_HOUSE_KEY)

        assert resolve_api_key(ref, required=False) is None

    def test_a_house_dotted_ref_still_resolves_from_env_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The narrowing is scoped to tenant refs only.

        OmniNode's own workloads on OmniNode's own key are not pooling, and the
        platform's lanes resolve house refs from their configured source. A
        change that broke this would be a regression dressed as a fix.
        """
        monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
        monkeypatch.setenv("LLM_GLM_API_KEY", _FAKE_HOUSE_KEY)

        resolved = resolve_api_key("llm.glm.api_key", required=False)

        assert resolved is not None
        assert resolved.get_secret_value() == _FAKE_HOUSE_KEY


class TestAc1CustomerKeyResolvesAndRoutes:
    """AC1: a delegation configured with a customer-supplied OpenRouter key
    executes against OpenRouter and returns a receipt naming that route.

    Falsifier: a receipt naming any other route, or naming no route at all.
    """

    def test_registered_credential_substitutes_the_house_rung(
        self, local_db: Path
    ) -> None:
        ref = register_local_byok_credential(
            "openrouter", _FAKE_CUSTOMER_KEY, db_path=local_db
        )

        routed = substitute_local_byok_route(_house_rung(), db_path=local_db)

        # The route that answers is the declared BYOK backend, not a house rung.
        assert routed.backend_id == "byok-openrouter"
        assert routed.endpoint_ref.startswith("https://openrouter.ai/")
        # It carries the CUSTOMER's minted reference.
        assert routed.secret_ref == ref
        assert is_tenant_credential_ref(routed.secret_ref)
        # And declares no environment fallback for a later call site to thread.
        assert routed.api_key_env is None

    def test_the_substituted_route_resolves_the_customer_value_by_reference(
        self, local_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end within the process: ref -> local store -> value.

        The house key is present in the environment throughout, which is what
        makes the assertion meaningful: the value that comes back is the one in
        the store, and it is not the one in the environment.
        """
        monkeypatch.setenv("OPENROUTER_API_KEY", _FAKE_HOUSE_KEY)
        ref = register_local_byok_credential(
            "openrouter", _FAKE_CUSTOMER_KEY, db_path=local_db
        )

        resolved = asyncio.run(LocalByokCredentialStore(local_db).get_secret(ref))

        assert resolved == _FAKE_CUSTOMER_KEY
        assert resolved != _FAKE_HOUSE_KEY

    def test_the_route_names_the_artifact_that_declared_it(
        self, local_db: Path
    ) -> None:
        """The receipt has to name a route, which means the route has to say where
        it came from -- the AC1 falsifier is a receipt naming no route."""
        register_local_byok_credential(
            "openrouter", _FAKE_CUSTOMER_KEY, db_path=local_db
        )

        routed = substitute_local_byok_route(_house_rung(), db_path=local_db)

        assert "byok_provider_backends.v1.yaml" in routed.model_id_source
        assert "openrouter" in routed.model_id_source


class TestAc3HouseCredentialIsUnreachable:
    """AC3: the house OpenRouter credential is provably unreachable from this path.

    Falsifier: a run that succeeds with the customer key absent, which would
    prove a house credential answered.
    """

    def test_no_registered_credential_means_no_byok_route(self, local_db: Path) -> None:
        """With nothing registered the house rung is handed back untouched.

        This module does not manufacture a refusal: the house ref then fails to
        resolve at the secret boundary on a machine that has no house key. What
        it must never do is invent a customer route out of a house one.
        """
        routed = substitute_local_byok_route(_house_rung(), db_path=local_db)

        assert routed.backend_id == "openrouter-qwen3-coder-480b"
        assert routed.secret_ref == "llm.openrouter.api_key"

    def test_the_customer_ref_does_not_fall_back_to_the_house_value(
        self, local_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The AC3 falsifier, run directly: customer key absent, house key present."""
        monkeypatch.setenv("OPENROUTER_API_KEY", _FAKE_HOUSE_KEY)
        monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
        register_local_byok_credential(
            "openrouter", _FAKE_CUSTOMER_KEY, db_path=local_db
        )
        routed = substitute_local_byok_route(_house_rung(), db_path=local_db)
        assert routed.secret_ref is not None

        # Now revoke the customer's key. The house value is still in the
        # environment. Resolution must fail rather than borrow it.
        revoke_local_byok_credential("openrouter", db_path=local_db)
        store = LocalByokCredentialStore(local_db)

        assert asyncio.run(store.get_secret(routed.secret_ref)) is None
        with pytest.raises(SecretResolutionError):
            resolve_api_key(routed.secret_ref, required=True)

    def test_a_substituted_route_never_carries_a_house_reference(
        self, local_db: Path
    ) -> None:
        register_local_byok_credential(
            "openrouter", _FAKE_CUSTOMER_KEY, db_path=local_db
        )

        routed = substitute_local_byok_route(_house_rung(), db_path=local_db)

        assert routed.secret_ref is not None
        assert not routed.secret_ref.startswith("llm.")
        assert house_provider_slug(routed.secret_ref) is None


class TestSubstitutionScope:
    """What the substitution must leave alone."""

    def test_a_keyless_local_rung_is_returned_unchanged(self, local_db: Path) -> None:
        """Cheapest-first is unaffected: a free rung on owned hardware still wins."""
        register_local_byok_credential(
            "openrouter", _FAKE_CUSTOMER_KEY, db_path=local_db
        )
        local_rung = _house_rung(secret_ref=None)

        assert substitute_local_byok_route(local_rung, db_path=local_db) is local_rung

    def test_a_provider_the_catalogue_does_not_offer_is_not_substituted(
        self, local_db: Path
    ) -> None:
        """``vertex`` is declared not_offered; a house vertex rung stays a house rung."""
        register_local_byok_credential("vertex", _FAKE_CUSTOMER_KEY, db_path=local_db)
        rung = _house_rung(secret_ref="llm.vertex.access_token")

        assert substitute_local_byok_route(rung, db_path=local_db) is rung

    def test_an_already_tenant_shaped_ref_is_not_substituted_again(
        self, local_db: Path
    ) -> None:
        register_local_byok_credential(
            "openrouter", _FAKE_CUSTOMER_KEY, db_path=local_db
        )
        already = _house_rung(secret_ref=f"cred_someone_openrouter_{'d' * 32}")

        assert substitute_local_byok_route(already, db_path=local_db) is already


class TestLocalStoreBehaviour:
    """The store half (OMN-18695's read, implemented minimally here)."""

    def test_an_absent_database_resolves_nothing_and_does_not_raise(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / "nope" / "delegation.sqlite"

        assert resolve_local_byok_credential_ref("openrouter", db_path=missing) is None
        assert registered_local_byok_providers(db_path=missing) == ()
        assert asyncio.run(LocalByokCredentialStore(missing).get_secret("x")) is None

    def test_the_minted_reference_carries_no_secret_material(
        self, local_db: Path
    ) -> None:
        ref = register_local_byok_credential(
            "openrouter", _FAKE_CUSTOMER_KEY, db_path=local_db
        )

        assert _FAKE_CUSTOMER_KEY not in ref
        assert ref.startswith(f"cred_{LOCAL_INSTALL_TENANT_ID}_openrouter_")
        assert is_tenant_credential_ref(ref)

    def test_listing_returns_references_never_values(self, local_db: Path) -> None:
        ref = register_local_byok_credential(
            "openrouter", _FAKE_CUSTOMER_KEY, db_path=local_db
        )

        keys = asyncio.run(LocalByokCredentialStore(local_db).list_keys())

        assert keys == [ref]
        assert all(_FAKE_CUSTOMER_KEY not in key for key in keys)

    def test_re_registering_replaces_rather_than_accumulates(
        self, local_db: Path
    ) -> None:
        first = register_local_byok_credential(
            "openrouter", _FAKE_CUSTOMER_KEY, db_path=local_db
        )
        second = register_local_byok_credential(
            "openrouter", _FAKE_CUSTOMER_KEY + "2", db_path=local_db
        )

        assert first != second
        assert (
            resolve_local_byok_credential_ref("openrouter", db_path=local_db) == second
        )
        assert asyncio.run(LocalByokCredentialStore(local_db).get_secret(first)) is None

    def test_a_blank_value_is_refused_rather_than_registered(
        self, local_db: Path
    ) -> None:
        """A registered reference that can never resolve is worse than none."""
        with pytest.raises(LocalByokCredentialError):
            register_local_byok_credential("openrouter", "   ", db_path=local_db)

        assert resolve_local_byok_credential_ref("openrouter", db_path=local_db) is None

    def test_revoking_reports_whether_anything_was_there(self, local_db: Path) -> None:
        assert revoke_local_byok_credential("openrouter", db_path=local_db) == 0
        register_local_byok_credential(
            "openrouter", _FAKE_CUSTOMER_KEY, db_path=local_db
        )
        assert revoke_local_byok_credential("openrouter", db_path=local_db) == 1
