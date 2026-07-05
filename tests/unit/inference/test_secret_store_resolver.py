# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the secret-store resolver at the inference effect boundary (OMN-12824).

The resolver replaces direct ``os.environ`` reads with resolution through the
canonical ``ProtocolSecretStore``. These tests pin the contract:

* an ``api_key_ref`` resolves its VALUE through the store (env-backed default),
* a ``None`` reference resolves to ``None`` (unauthenticated backend),
* a declared-but-missing reference fails closed (raise, never default),
* an Infisical-style injected store is honored without code change,
* the resolved value is a ``SecretStr`` (never a bare printable string),
* the sync wrapper rejects being called from a running event loop.
"""

from __future__ import annotations

import asyncio
import textwrap

import pytest
from omnibase_spi.protocols.services import ProtocolSecretStore
from pydantic import SecretStr

from omnimarket.inference.secret_store_resolver import (
    SecretResolutionError,
    api_key_ref_available,
    clear_secret_store_resolver_cache,
    resolve_api_key,
    resolve_api_key_async,
    resolve_api_key_loop_safe,
)


@pytest.fixture(autouse=True)
def _clear_configured_secret_store_cache() -> None:
    clear_secret_store_resolver_cache()


class _FakeSecretStore:
    """In-memory ``ProtocolSecretStore`` standing in for an Infisical backend."""

    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets

    async def get_secret(self, key: str) -> str | None:
        return self._secrets.get(key)

    async def set_secret(self, key: str, value: str) -> bool:
        raise RuntimeError("read-only fake")

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("read-only fake")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        keys = list(self._secrets.keys())
        if prefix is None:
            return keys
        return [k for k in keys if k.startswith(prefix)]

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        return None


class TestResolveApiKeyAsync:
    async def test_none_ref_resolves_to_none(self) -> None:
        assert await resolve_api_key_async(None) is None

    async def test_env_backed_default_resolves_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMN12824_TEST_KEY", "sk-from-env")
        resolved = await resolve_api_key_async("OMN12824_TEST_KEY")
        assert isinstance(resolved, SecretStr)
        assert resolved.get_secret_value() == "sk-from-env"

    async def test_missing_ref_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OMN12824_ABSENT_KEY", raising=False)
        with pytest.raises(SecretResolutionError, match="OMN12824_ABSENT_KEY"):
            await resolve_api_key_async("OMN12824_ABSENT_KEY")

    async def test_empty_value_fails_closed(self) -> None:
        store = _FakeSecretStore({"GEMINI_API_KEY": ""})
        with pytest.raises(SecretResolutionError, match="GEMINI_API_KEY"):
            await resolve_api_key_async("GEMINI_API_KEY", store=store)

    async def test_injected_store_is_honored(self) -> None:
        store = _FakeSecretStore({"OPENROUTER_API_KEY": "sk-from-infisical"})
        resolved = await resolve_api_key_async("OPENROUTER_API_KEY", store=store)
        assert isinstance(resolved, SecretStr)
        assert resolved.get_secret_value() == "sk-from-infisical"

    async def test_logical_ref_resolves_through_configured_mapping(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("OPEN_ROUTER_API_KEY", "sk-from-lane-overlay")
        config_file = tmp_path / "secret_resolver.yaml"
        config_file.write_text(
            textwrap.dedent("""\
                mappings:
                  - logical_name: llm.openrouter.api_key
                    source:
                      source_type: env
                      source_path: OPEN_ROUTER_API_KEY
            """)
        )
        monkeypatch.setenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", str(config_file))
        clear_secret_store_resolver_cache()

        resolved = await resolve_api_key_async("llm.openrouter.api_key")

        assert isinstance(resolved, SecretStr)
        assert resolved.get_secret_value() == "sk-from-lane-overlay"

    async def test_injected_store_satisfies_protocol(self) -> None:
        store: ProtocolSecretStore = _FakeSecretStore({"K": "v"})
        assert isinstance(store, ProtocolSecretStore)

    async def test_env_var_fallback_resolves_when_primary_ref_misses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-13943: a dotted secret_ref whose store lookup misses still
        resolves through the declared literal env_var_fallback name — the
        backend's own contract-declared ``api_key_env`` (e.g. the canonical
        ``OPEN_ROUTER_API_KEY`` / ``GEMINI_API_KEY`` already defined in
        ``~/.omnibase/.env``, distinct from the dotted convention's
        ``LLM_*_API_KEY`` mapping)."""
        store = _FakeSecretStore({})  # dotted ref never resolves
        monkeypatch.setenv("OMN13943_CANONICAL_KEY", "sk-canonical-fallback")

        resolved = await resolve_api_key_async(
            "llm.openrouter.api_key",
            store=store,
            env_var_fallback="OMN13943_CANONICAL_KEY",
        )

        assert isinstance(resolved, SecretStr)
        assert resolved.get_secret_value() == "sk-canonical-fallback"

    async def test_env_var_fallback_not_consulted_when_primary_ref_resolves(
        self,
    ) -> None:
        """The fallback is a LAST RESORT — it never overrides a resolvable
        primary ref, even when both are set."""
        store = _FakeSecretStore({"llm.glm.api_key": "sk-primary"})

        resolved = await resolve_api_key_async(
            "llm.glm.api_key",
            store=store,
            env_var_fallback="OMN13943_SHOULD_NOT_BE_USED",
        )

        assert isinstance(resolved, SecretStr)
        assert resolved.get_secret_value() == "sk-primary"

    async def test_missing_ref_fails_closed_even_with_unset_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-fast (Rule 8): a declared env_var_fallback that is ALSO unset
        must still raise — no silent default, no partial fallback chain."""
        store = _FakeSecretStore({})
        monkeypatch.delenv("OMN13943_ALSO_ABSENT", raising=False)

        with pytest.raises(SecretResolutionError, match=r"llm\.openrouter\.api_key"):
            await resolve_api_key_async(
                "llm.openrouter.api_key",
                store=store,
                env_var_fallback="OMN13943_ALSO_ABSENT",
            )


class TestResolveApiKeySync:
    def test_none_ref_resolves_to_none(self) -> None:
        assert resolve_api_key(None) is None

    def test_env_backed_default_resolves_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMN12824_SYNC_KEY", "sk-sync")
        resolved = resolve_api_key("OMN12824_SYNC_KEY")
        assert isinstance(resolved, SecretStr)
        assert resolved.get_secret_value() == "sk-sync"

    def test_missing_ref_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OMN12824_SYNC_ABSENT", raising=False)
        with pytest.raises(SecretResolutionError):
            resolve_api_key("OMN12824_SYNC_ABSENT")

    def test_rejects_running_event_loop(self) -> None:
        async def _inner() -> None:
            with pytest.raises(RuntimeError, match="sync-only"):
                resolve_api_key("ANY_KEY")

        asyncio.run(_inner())


class TestResolveApiKeyLoopSafe:
    """``resolve_api_key_loop_safe`` (OMN-13843): a sync resolver that returns the
    secret VALUE and does not raise the sync-only guard when a loop is running."""

    def test_none_ref_resolves_to_none(self) -> None:
        assert resolve_api_key_loop_safe(None) is None

    def test_env_backed_value_no_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMN13843_LOOPSAFE_KEY", "sk-loopsafe")
        resolved = resolve_api_key_loop_safe("OMN13843_LOOPSAFE_KEY")
        assert isinstance(resolved, SecretStr)
        assert resolved.get_secret_value() == "sk-loopsafe"

    def test_resolves_from_within_running_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point: called from inside a running loop it must NOT raise
        the sync-only error — it offloads to a worker thread and returns a value.
        """
        monkeypatch.setenv("OMN13843_LOOPSAFE_KEY", "sk-in-loop")

        async def _inner() -> SecretStr | None:
            return resolve_api_key_loop_safe("OMN13843_LOOPSAFE_KEY")

        resolved = asyncio.run(_inner())
        assert isinstance(resolved, SecretStr)
        assert resolved.get_secret_value() == "sk-in-loop"

    def test_missing_ref_fails_closed_within_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OMN13843_LOOPSAFE_ABSENT", raising=False)

        async def _inner() -> None:
            with pytest.raises(SecretResolutionError, match="OMN13843_LOOPSAFE_ABSENT"):
                resolve_api_key_loop_safe("OMN13843_LOOPSAFE_ABSENT")

        asyncio.run(_inner())

    def test_not_required_within_loop_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OMN13843_LOOPSAFE_ABSENT", raising=False)

        async def _inner() -> SecretStr | None:
            return resolve_api_key_loop_safe("OMN13843_LOOPSAFE_ABSENT", required=False)

        assert asyncio.run(_inner()) is None


class TestApiKeyRefAvailable:
    def test_none_ref_is_available(self) -> None:
        assert api_key_ref_available(None) is True

    def test_missing_ref_is_unavailable(self) -> None:
        store = _FakeSecretStore({})
        assert api_key_ref_available("OPENROUTER_API_KEY", store=store) is False

    def test_empty_ref_is_unavailable(self) -> None:
        store = _FakeSecretStore({"OPENROUTER_API_KEY": ""})
        assert api_key_ref_available("OPENROUTER_API_KEY", store=store) is False

    def test_present_ref_is_available(self) -> None:
        store = _FakeSecretStore({"OPENROUTER_API_KEY": "sk"})
        assert api_key_ref_available("OPENROUTER_API_KEY", store=store) is True

    async def test_present_ref_is_available_from_async_context(self) -> None:
        store = _FakeSecretStore({"OPENROUTER_API_KEY": "sk"})
        assert api_key_ref_available("OPENROUTER_API_KEY", store=store) is True

    async def test_missing_ref_is_unavailable_from_async_context(self) -> None:
        store = _FakeSecretStore({})
        assert api_key_ref_available("OPENROUTER_API_KEY", store=store) is False

    def test_env_var_fallback_makes_a_ref_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-13943: routing-tier eligibility must agree with what the effect
        boundary will actually resolve — a backend whose dotted secret_ref
        convention misses but whose own literal env var IS set must be
        reported available, or a reachable tier is wrongly excluded."""
        store = _FakeSecretStore({})
        monkeypatch.setenv("OMN13943_AVAILABLE_KEY", "sk-present")

        assert (
            api_key_ref_available(
                "llm.openrouter.api_key",
                store=store,
                env_var_fallback="OMN13943_AVAILABLE_KEY",
            )
            is True
        )

    def test_env_var_fallback_unset_stays_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _FakeSecretStore({})
        monkeypatch.delenv("OMN13943_UNAVAILABLE_KEY", raising=False)

        assert (
            api_key_ref_available(
                "llm.openrouter.api_key",
                store=store,
                env_var_fallback="OMN13943_UNAVAILABLE_KEY",
            )
            is False
        )


class TestProviderNativeAliasStoreLevel:
    """OMN-13960: the DEFAULT delegation store accepts provider-native env-var
    names as aliases, so OpenRouter/Gemini secrets resolve from ANY call site
    WITHOUT threading the per-backend ``env_var_fallback`` (``api_key_env``).

    ~/.omnibase/.env carries ``OPEN_ROUTER_API_KEY`` / ``GEMINI_API_KEY`` — names
    that do NOT match the dotted-ref → ``LLM_*_API_KEY`` convention. OMN-13943
    only threaded these at two call sites; the LLM-judge adapter did not, so an
    OpenRouter/Gemini-backed judge would fail-closed. These tests exercise the
    DEFAULT store (no injected store, NO ``env_var_fallback``) so the store-level
    alias is what resolves.
    """

    def _isolate_default_store(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force the default _ConventionFallbackSecretStore (no lane config), and
        # remove the convention/literal names so ONLY the provider-native alias
        # can satisfy the lookup.
        monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
        monkeypatch.delenv("LLM_OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("LLM_GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("llm.openrouter.api_key", raising=False)
        monkeypatch.delenv("llm.gemini.api_key", raising=False)
        clear_secret_store_resolver_cache()

    async def test_openrouter_ref_resolves_via_provider_native_alias(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._isolate_default_store(monkeypatch)
        monkeypatch.setenv("OPEN_ROUTER_API_KEY", "sk-or-provider-native")

        resolved = await resolve_api_key_async("llm.openrouter.api_key")

        assert isinstance(resolved, SecretStr)
        assert resolved.get_secret_value() == "sk-or-provider-native"

    async def test_gemini_ref_resolves_via_provider_native_alias(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._isolate_default_store(monkeypatch)
        monkeypatch.setenv("GEMINI_API_KEY", "sk-gemini-provider-native")

        resolved = await resolve_api_key_async("llm.gemini.api_key")

        assert isinstance(resolved, SecretStr)
        assert resolved.get_secret_value() == "sk-gemini-provider-native"

    async def test_convention_name_wins_over_alias(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dotted → ``LLM_*_API_KEY`` convention resolves BEFORE the alias, so
        an explicit ``LLM_OPENROUTER_API_KEY`` still takes precedence."""
        monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
        clear_secret_store_resolver_cache()
        monkeypatch.setenv("LLM_OPENROUTER_API_KEY", "sk-convention")
        monkeypatch.setenv("OPEN_ROUTER_API_KEY", "sk-alias-should-not-win")

        resolved = await resolve_api_key_async("llm.openrouter.api_key")

        assert isinstance(resolved, SecretStr)
        assert resolved.get_secret_value() == "sk-convention"

    async def test_alias_absent_still_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-fast (Rule 8): when NEITHER the convention name NOR the
        provider-native alias is set, a required lookup still raises — the alias
        is a last-resort accept, never a silent default."""
        self._isolate_default_store(monkeypatch)
        monkeypatch.delenv("OPEN_ROUTER_API_KEY", raising=False)

        with pytest.raises(SecretResolutionError, match=r"llm\.openrouter\.api_key"):
            await resolve_api_key_async("llm.openrouter.api_key")

    async def test_non_provider_ref_unaffected_by_alias_map(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ref with no provider-native alias entry is unchanged: it still fails
        closed when its convention name is unset."""
        self._isolate_default_store(monkeypatch)
        monkeypatch.delenv("LLM_GLM_API_KEY", raising=False)

        with pytest.raises(SecretResolutionError, match=r"llm\.glm\.api_key"):
            await resolve_api_key_async("llm.glm.api_key")
