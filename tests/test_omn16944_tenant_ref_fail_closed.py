# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16944 AC2/AC3: a tenant-minted ref can never reach a house key.

Before this change the tenant path was fail-closed only by accident of DTO
shape: ``handler_llm_delegation_call._resolve_outbound_headers`` passes
``env_var_fallback=request.api_key_env`` on EVERY ref, and it is only because
``ModelInferenceIntent`` carries no ``api_key_env`` and
``_decision_from_tenant_overlay`` threads none that a tenant ref never picked up
a platform env var. ``resolve_tenant_scoped_api_key_async`` -- written for
exactly this in OMN-15631 -- had ZERO production call sites.

These tests pin the guarantee at the choke point instead: a ref carrying the
minted tenant-credential shape drops ``env_var_fallback`` unconditionally and
routes through the tenant-scoped resolver, whatever the call site passes.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from omnibase_spi.protocols.services import ProtocolSecretStore

from omnimarket.inference.secret_store_resolver import (
    SecretResolutionError,
    api_key_ref_available,
    resolve_api_key_async,
    resolve_api_key_loop_safe,
)
from omnimarket.projection.credential_publisher import mint_api_key_ref
from omnimarket.tenant_credential_ref import (
    TENANT_CREDENTIAL_LANE_REF_PATTERN,
    is_tenant_credential_ref,
    tenant_hint_from_ref,
)

pytestmark = pytest.mark.unit

_HOUSE_ENV_VAR = "OPENROUTER_API_KEY"
_HOUSE_KEY_VALUE = "house-key-that-must-never-be-handed-to-a-tenant"


class _EmptyStore:
    """A store that holds nothing -- the state of a lane with no BYOK value."""

    async def get_secret(self, key: str) -> str | None:
        del key
        return None

    async def set_secret(self, key: str, value: str) -> bool:
        raise RuntimeError("read-only")

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("read-only")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        del prefix
        return []

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds


class _SingleValueStore(_EmptyStore):
    def __init__(self, key: str, value: str) -> None:
        self._key = key
        self._value = value

    async def get_secret(self, key: str) -> str | None:
        return self._value if key == self._key else None


def _store(store: object) -> ProtocolSecretStore:
    return store  # type: ignore[return-value]


class TestMintedShapeIsRecognisedByOneAuthority:
    """The minter and the boundary matcher must not be able to drift apart."""

    def test_every_minted_ref_is_recognised_as_tenant_scoped(self) -> None:
        for tenant_id, provider in (
            ("acme-corp", "openrouter"),
            ("tenant_with_underscores", "open_router"),
            ("omninode", "gemini"),
            ("t.1:2-3", "openai"),
        ):
            ref = mint_api_key_ref(tenant_id, provider)
            assert is_tenant_credential_ref(ref), ref

    def test_platform_refs_are_not_claimed(self) -> None:
        for ref in (
            "llm.openrouter.api_key",
            "gateway.attach.keycloak.client_secret",
            "credentials.house.api_key",
            "cred_missing_uuid_suffix",
        ):
            assert not is_tenant_credential_ref(ref), ref

    def test_tenant_hint_is_derived_for_attribution(self) -> None:
        ref = mint_api_key_ref("acme-corp", "openrouter")
        assert tenant_hint_from_ref(ref) == "acme-corp"

    def test_lane_pattern_claims_exactly_the_minted_shape(self) -> None:
        """The lane-declared pattern must claim what the minter emits.

        ``TENANT_CREDENTIAL_LANE_REF_PATTERN`` is the literal a lane declares as
        a ``ModelSecretNamespaceRule.ref_pattern`` (omnibase_infra, OMN-16944).
        It is exercised here with the stdlib matcher the rule uses, so this test
        pins the producer/lane seam without taking a version dependency on an
        unreleased omnibase_infra.
        """
        lane = re.compile(TENANT_CREDENTIAL_LANE_REF_PATTERN)
        # A lane rule must be anchored and must not be a catch-all -- the two
        # structural constraints ModelSecretNamespaceRule enforces.
        assert TENANT_CREDENTIAL_LANE_REF_PATTERN.startswith("^")
        assert TENANT_CREDENTIAL_LANE_REF_PATTERN.endswith("$")
        assert lane.fullmatch("") is None

        for tenant_id, provider in (
            ("acme-corp", "openrouter"),
            ("tenant_with_underscores", "open_router"),
        ):
            ref = mint_api_key_ref(tenant_id, provider)
            assert lane.fullmatch(ref) is not None, ref
            assert is_tenant_credential_ref(ref)
        assert lane.fullmatch("llm.openrouter.api_key") is None


class TestTenantRefNeverResolvesToAHouseKey:
    """AC2 -- plant a house key, prove the tenant ref cannot reach it."""

    def test_env_var_fallback_is_dropped_for_a_tenant_ref(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_HOUSE_ENV_VAR, _HOUSE_KEY_VALUE)
        ref = mint_api_key_ref("acme-corp", "openrouter")

        with pytest.raises(SecretResolutionError) as excinfo:
            asyncio.run(
                resolve_api_key_async(
                    ref,
                    store=_store(_EmptyStore()),
                    required=True,
                    # A call site that DOES thread a house env var -- the exact
                    # shape handler_llm_delegation_call uses today.
                    env_var_fallback=_HOUSE_ENV_VAR,
                )
            )

        message = str(excinfo.value)
        assert "acme-corp" in message
        assert "never fall back to a house key" in message
        assert _HOUSE_KEY_VALUE not in message
        assert _HOUSE_ENV_VAR not in message

    def test_loop_safe_sync_boundary_drops_the_fallback_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sync effect boundary must not be a second, weaker door."""
        monkeypatch.setenv(_HOUSE_ENV_VAR, _HOUSE_KEY_VALUE)
        ref = mint_api_key_ref("acme-corp", "openrouter")

        with pytest.raises(SecretResolutionError):
            resolve_api_key_loop_safe(
                ref,
                store=_store(_EmptyStore()),
                required=True,
                env_var_fallback=_HOUSE_ENV_VAR,
            )

    def test_a_platform_ref_still_uses_its_declared_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The narrowing applies to tenant refs ONLY -- no house-path regression."""
        monkeypatch.setenv(_HOUSE_ENV_VAR, _HOUSE_KEY_VALUE)
        resolved = asyncio.run(
            resolve_api_key_async(
                "llm.openrouter.api_key",
                store=_store(_EmptyStore()),
                required=True,
                env_var_fallback=_HOUSE_ENV_VAR,
            )
        )
        assert resolved is not None
        assert resolved.get_secret_value() == _HOUSE_KEY_VALUE

    def test_availability_probe_on_a_tenant_ref_reports_false_not_house_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Routing-time eligibility must not be satisfied by a house key."""
        monkeypatch.setenv(_HOUSE_ENV_VAR, _HOUSE_KEY_VALUE)
        ref = mint_api_key_ref("acme-corp", "openrouter")
        assert (
            api_key_ref_available(
                ref,
                store=_store(_EmptyStore()),
                env_var_fallback=_HOUSE_ENV_VAR,
            )
            is False
        )


class TestTenantRefResolvesFromItsOwnStoredValue:
    """The narrowing must not break the case the feature exists for."""

    def test_stored_tenant_value_resolves(self) -> None:
        ref = mint_api_key_ref("acme-corp", "openrouter")
        resolved = asyncio.run(
            resolve_api_key_async(
                ref,
                store=_store(_SingleValueStore(ref, "tenant-supplied-key")),
                required=True,
                env_var_fallback=_HOUSE_ENV_VAR,
            )
        )
        assert resolved is not None
        assert resolved.get_secret_value() == "tenant-supplied-key"

    def test_availability_probe_is_true_when_the_tenant_value_exists(self) -> None:
        ref = mint_api_key_ref("acme-corp", "openrouter")
        assert (
            api_key_ref_available(
                ref, store=_store(_SingleValueStore(ref, "tenant-supplied-key"))
            )
            is True
        )
