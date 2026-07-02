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
