# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The customer's provider key resolves from the local store, never from env (OMN-18695).

Gap B of the local-path epic OMN-18693. OMN-17372 deleted ``api_key_env`` from
every backend in ``bifrost_delegation.yaml`` on the stated rule that
``secret_ref`` is a backend's only credential surface. That closed the CONTRACT
half. This suite pins the RESOLVER half: on the local path a declared provider
``secret_ref`` is answered by the machine's own SQLite store and by nothing
else, so a value sitting in ``LLM_OPENROUTER_API_KEY`` can no longer
authenticate a call.

The tests that matter most here are the ones with the env var SET. A test that
merely shows the store working proves nothing about whether env was consulted
first; the refusal-with-env-present tests are the ones that fail if the
fallback comes back.
"""

from __future__ import annotations

import asyncio
import sqlite3
import stat
from pathlib import Path

import pytest

from omnimarket.inference.local_byok_credential_adapter import (
    LOCAL_CREDENTIAL_TABLE,
    LocalByokCredentialStore,
    is_local_store_only_ref,
)
from omnimarket.inference.secret_store_resolver import (
    LocalSecretNotRegisteredError,
    SecretResolutionError,
    resolve_api_key,
    resolve_api_key_async,
)

pytestmark = pytest.mark.unit

# A real contract-declared ref (bifrost_delegation.yaml:634) and the env var the
# retired convention fallback would have mapped it to.
_OPENROUTER_REF = "llm.openrouter.api_key"
_CONVENTION_ENV = "LLM_OPENROUTER_API_KEY"
_PROVIDER_NATIVE_ENV = "OPENROUTER_API_KEY"

_ENV_VALUE = "sk-from-env-must-never-be-used"
_STORE_VALUE = "sk-from-store"


@pytest.fixture(autouse=True)
def local_store_at_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the default local secret store at a per-test database file.

    Patched on the ``local_byok_credential_adapter`` module rather than on the
    resolver so
    every caller of the default path -- resolver, CLI, store -- agrees on one
    location within a test, the way they agree on one location on a machine.
    """
    db_path = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )
    monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
    from omnimarket.inference.secret_store_resolver import (
        clear_secret_store_resolver_cache,
    )

    clear_secret_store_resolver_cache()
    return db_path


@pytest.fixture
def _env_key_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set every env var the retired fallback chain would have read."""
    monkeypatch.setenv(_CONVENTION_ENV, _ENV_VALUE)
    monkeypatch.setenv(_PROVIDER_NATIVE_ENV, _ENV_VALUE)
    monkeypatch.setenv(_OPENROUTER_REF, _ENV_VALUE)


class TestEnvIsNeverConsulted:
    """The env vars are SET in every test here. That is the point."""

    @pytest.mark.usefixtures("_env_key_present")
    async def test_empty_store_refuses_although_env_holds_a_value(self) -> None:
        with pytest.raises(LocalSecretNotRegisteredError) as excinfo:
            await resolve_api_key_async(_OPENROUTER_REF)
        message = str(excinfo.value)
        assert _OPENROUTER_REF in message
        assert "onex secret set" in message

    @pytest.mark.usefixtures("_env_key_present")
    async def test_refusal_never_prints_the_env_value(self) -> None:
        with pytest.raises(LocalSecretNotRegisteredError) as excinfo:
            await resolve_api_key_async(_OPENROUTER_REF)
        assert _ENV_VALUE not in str(excinfo.value)
        assert _ENV_VALUE not in repr(excinfo.value)

    @pytest.mark.usefixtures("_env_key_present")
    async def test_store_value_wins_over_a_differing_env_value(self) -> None:
        store = LocalByokCredentialStore()
        await store.set_secret(_OPENROUTER_REF, _STORE_VALUE)

        resolved = await resolve_api_key_async(_OPENROUTER_REF)

        assert resolved is not None
        assert resolved.get_secret_value() == _STORE_VALUE

    @pytest.mark.usefixtures("_env_key_present")
    async def test_deleting_the_store_entry_restores_the_refusal(self) -> None:
        store = LocalByokCredentialStore()
        await store.set_secret(_OPENROUTER_REF, _STORE_VALUE)
        assert await store.delete_secret(_OPENROUTER_REF) is True

        with pytest.raises(LocalSecretNotRegisteredError):
            await resolve_api_key_async(_OPENROUTER_REF)

    @pytest.mark.usefixtures("_env_key_present")
    async def test_declared_env_var_fallback_cannot_rescue_a_provider_ref(
        self,
    ) -> None:
        """A caller that still threads ``env_var_fallback`` gets no rescue.

        ``api_key_env`` is gone from the contract (OMN-17372) but the parameter
        survives on the resolver for non-provider refs. A provider ref must
        refuse even when a call site passes one, so the guarantee is a property
        of the ref rather than of every call site remembering.
        """
        with pytest.raises(LocalSecretNotRegisteredError):
            await resolve_api_key_async(
                _OPENROUTER_REF, env_var_fallback=_PROVIDER_NATIVE_ENV
            )

    @pytest.mark.usefixtures("_env_key_present")
    def test_sync_resolver_refuses_on_the_same_terms(self) -> None:
        with pytest.raises(LocalSecretNotRegisteredError):
            resolve_api_key(_OPENROUTER_REF)

    @pytest.mark.usefixtures("_env_key_present")
    async def test_not_required_returns_none_rather_than_an_env_value(self) -> None:
        assert await resolve_api_key_async(_OPENROUTER_REF, required=False) is None


class TestRefClassification:
    """Which refs the local store owns, and which keep their existing path."""

    @pytest.mark.parametrize(
        "ref",
        [
            "llm.openrouter.api_key",
            "llm.glm.api_key",
            "llm.gemini.api_key",
            "llm.vertex.access_token",
            "cred_acme_openrouter_" + "0" * 32,
        ],
    )
    def test_provider_and_tenant_refs_are_store_only(self, ref: str) -> None:
        assert is_local_store_only_ref(ref) is True

    @pytest.mark.parametrize("ref", ["GITHUB_TOKEN", "SOME_LITERAL", "", None])
    def test_other_refs_are_not_claimed(self, ref: str | None) -> None:
        assert is_local_store_only_ref(ref) is False

    async def test_non_provider_ref_still_resolves_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Scope guard: this change is about PROVIDER keys.

        ``GITHUB_TOKEN`` reaches the resolver from a CI environment that has no
        local store, and re-homing it is a different question with a different
        blast radius. If this test ever fails, the change has widened past its
        ticket.
        """
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_notaproviderkey0000000000")
        resolved = await resolve_api_key_async(
            "GITHUB_TOKEN", env_var_fallback="GITHUB_TOKEN"
        )
        assert resolved is not None

    async def test_tenant_ref_refusal_is_the_typed_local_one(self) -> None:
        tenant_ref = "cred_acme_openrouter_" + "a" * 32
        with pytest.raises(LocalSecretNotRegisteredError) as excinfo:
            await resolve_api_key_async(tenant_ref)
        assert tenant_ref in str(excinfo.value)

    def test_local_refusal_is_a_secret_resolution_error(self) -> None:
        """Existing ``except SecretResolutionError`` call sites keep working."""
        assert issubclass(LocalSecretNotRegisteredError, SecretResolutionError)


class TestStoreBehaviour:
    """The store itself: writes, overwrites, listing, and file mode."""

    async def test_set_then_get_round_trips(self) -> None:
        store = LocalByokCredentialStore()
        assert await store.set_secret(_OPENROUTER_REF, _STORE_VALUE) is True
        assert await store.get_secret(_OPENROUTER_REF) == _STORE_VALUE

    async def test_absent_ref_returns_none_rather_than_raising(self) -> None:
        assert (
            await LocalByokCredentialStore().get_secret("llm.nothing.api_key") is None
        )

    async def test_absent_database_returns_none(self, local_store_at_tmp: Path) -> None:
        assert not local_store_at_tmp.exists()
        assert await LocalByokCredentialStore().get_secret(_OPENROUTER_REF) is None

    async def test_overwrite_replaces_the_value(self) -> None:
        store = LocalByokCredentialStore()
        await store.set_secret(_OPENROUTER_REF, "first")
        await store.set_secret(_OPENROUTER_REF, "second")

        assert await store.get_secret(_OPENROUTER_REF) == "second"
        assert await store.list_keys() == [_OPENROUTER_REF]

    async def test_list_keys_returns_refs_and_never_values(self) -> None:
        store = LocalByokCredentialStore()
        await store.set_secret(_OPENROUTER_REF, _STORE_VALUE)
        await store.set_secret("llm.glm.api_key", "sk-glm")

        keys = await store.list_keys()

        assert keys == ["llm.glm.api_key", _OPENROUTER_REF]
        assert all(_STORE_VALUE not in key for key in keys)

    async def test_list_keys_honours_a_prefix(self) -> None:
        store = LocalByokCredentialStore()
        await store.set_secret(_OPENROUTER_REF, _STORE_VALUE)
        await store.set_secret("other.ref", "v")

        assert await store.list_keys(prefix="llm.") == [_OPENROUTER_REF]

    async def test_delete_reports_whether_anything_was_removed(self) -> None:
        store = LocalByokCredentialStore()
        await store.set_secret(_OPENROUTER_REF, _STORE_VALUE)

        assert await store.delete_secret(_OPENROUTER_REF) is True
        assert await store.delete_secret(_OPENROUTER_REF) is False

    async def test_an_empty_value_is_refused_rather_than_stored(self) -> None:
        """An empty entry is indistinguishable from an absent one at resolution.

        The store reports the refusal rather than raising; the command surface
        raises, because a customer piping nothing in needs to be told.
        """
        store = LocalByokCredentialStore()

        assert await store.set_secret(_OPENROUTER_REF, "") is False
        assert await store.get_secret(_OPENROUTER_REF) is None

    async def test_write_leaves_the_database_owner_only(
        self, local_store_at_tmp: Path
    ) -> None:
        await LocalByokCredentialStore().set_secret(_OPENROUTER_REF, _STORE_VALUE)

        mode = stat.S_IMODE(local_store_at_tmp.stat().st_mode)
        assert mode & 0o077 == 0, f"database is mode {mode:04o}, expected owner-only"

    async def test_write_tightens_a_pre_existing_world_readable_database(
        self, local_store_at_tmp: Path
    ) -> None:
        """The evidence projection may have created this file at the umask default."""
        local_store_at_tmp.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(local_store_at_tmp).close()
        local_store_at_tmp.chmod(0o644)

        await LocalByokCredentialStore().set_secret(_OPENROUTER_REF, _STORE_VALUE)

        assert stat.S_IMODE(local_store_at_tmp.stat().st_mode) & 0o077 == 0

    async def test_reading_a_stored_value_from_a_widened_file_is_refused(
        self, local_store_at_tmp: Path
    ) -> None:
        """A write-time chmod proves nothing about the file that is later read."""
        store = LocalByokCredentialStore()
        await store.set_secret(_OPENROUTER_REF, _STORE_VALUE)
        local_store_at_tmp.chmod(0o644)

        with pytest.raises(PermissionError) as excinfo:
            await store.get_secret(_OPENROUTER_REF)
        assert "chmod 600" in str(excinfo.value)
        assert _STORE_VALUE not in str(excinfo.value)

    async def test_a_widened_file_holding_no_entry_still_answers_none(
        self, local_store_at_tmp: Path
    ) -> None:
        """Refuse only when there is something to protect.

        The evidence database is shared, pre-existing and often 0644. A machine
        that has never registered a key must get the "not registered" refusal
        naming the command to run -- not a permission complaint about a file
        holding no secret.
        """
        store = LocalByokCredentialStore()
        await store.set_secret(_OPENROUTER_REF, _STORE_VALUE)
        await store.delete_secret(_OPENROUTER_REF)
        local_store_at_tmp.chmod(0o644)

        assert await store.get_secret(_OPENROUTER_REF) is None

    async def test_the_secrets_table_is_separate_from_delegation_events(
        self, local_store_at_tmp: Path
    ) -> None:
        """Secrets live in their own table in the machine's existing store."""
        await LocalByokCredentialStore().set_secret(_OPENROUTER_REF, _STORE_VALUE)

        conn = sqlite3.connect(local_store_at_tmp)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        assert LOCAL_CREDENTIAL_TABLE in tables

    async def test_concurrent_writes_do_not_lose_an_entry(self) -> None:
        store = LocalByokCredentialStore()
        refs = [f"llm.p{index}.api_key" for index in range(8)]

        await asyncio.gather(*(store.set_secret(ref, f"v{ref}") for ref in refs))

        assert await store.list_keys() == sorted(refs)


class TestInteractionWithTheTypedRefusalLane:
    """The refusal reaches OMN-18696's classification without either lane's help.

    OMN-18696 landed a ``CREDENTIAL_ABSENT`` refusal at the effect boundary by
    catching :class:`SecretResolutionError` ahead of the bare
    ``except Exception`` that used to classify the same fact ``UNKNOWN`` and
    therefore RETRYABLE. This ticket's refusal is a SUBCLASS of that error
    specifically so the two compose with no coupling between them.

    Asserted rather than assumed: the subclass relationship is the whole
    mechanism, and a future refactor that made this a sibling error would
    silently return an unregistered key to retryable-UNKNOWN, escalating the
    tier ladder over a configuration fact no higher tier can change.
    """

    def test_the_refusal_is_caught_by_the_effect_boundarys_handler(self) -> None:
        import inspect

        from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
            handler_llm_delegation_call as effect,
        )

        source = inspect.getsource(effect)
        assert "except SecretResolutionError" in source, (
            "the effect boundary no longer catches SecretResolutionError, so an "
            "unregistered provider key falls through to the bare except and is "
            "classified retryable-UNKNOWN again (OMN-18696 regression)"
        )

    def test_the_local_refusal_is_a_secret_resolution_error(self) -> None:
        assert issubclass(LocalSecretNotRegisteredError, SecretResolutionError)
