# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Secret-store resolution at the inference effect boundary (OMN-12824).

The routing authority carries only a *reference* to a secret (``api_key_ref``)
— the env-var / Infisical key name, never the value. The literal secret value
is resolved here, at the provider-call effect boundary, through the canonical
``ProtocolSecretStore`` (omnibase_spi). This replaces the direct
``os.environ[...]`` reads that previously coupled the handlers to environment
variables and bypassed the secret store.

Resolution is fail-closed: when a backend declares an ``api_key_ref`` but the
secret store has no value for it, resolution raises. Callers never substitute a
default and never fall back to an empty key.

Default store: ``AdapterEnvSecretStore`` (omnibase_infra) — reads from the
process environment. The same interface resolves from Infisical when an
Infisical-backed ``ProtocolSecretStore`` is injected (the canonical store at the
deployed effect boundary), so no handler code changes when the secret source
moves from env to Infisical.

Secret values are wrapped in ``SecretStr`` so they are never accidentally
printed, logged, or serialized.
"""

from __future__ import annotations

import asyncio
import os
from functools import lru_cache
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import cast

import yaml
from omnibase_infra.runtime.models.model_secret_resolver_config import (
    ModelSecretResolverConfig,
)
from omnibase_infra.runtime.secret_resolver import SecretResolver
from omnibase_infra.secret_stores import AdapterEnvSecretStore
from omnibase_spi.protocols.services import ProtocolSecretStore
from pydantic import SecretStr


class SecretResolutionError(RuntimeError):
    """Raised when a declared ``api_key_ref`` cannot be resolved fail-closed."""


class _MappedSecretStore:
    """``ProtocolSecretStore`` adapter over infra's logical secret resolver."""

    def __init__(self, resolver: SecretResolver) -> None:
        self._resolver = resolver

    async def get_secret(self, key: str) -> str | None:
        resolved = await self._resolver.get_secret_async(key, required=False)
        if resolved is None:
            return None
        return cast(str, resolved.get_secret_value())

    async def set_secret(self, key: str, value: str) -> bool:
        raise RuntimeError("Mapped secret store is read-only")

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("Mapped secret store is read-only")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        del prefix
        return []

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds
        return


@lru_cache(maxsize=1)
def _configured_secret_store() -> ProtocolSecretStore | None:
    """Return a lane-configured logical secret store when one is declared."""
    config_path = os.environ.get(  # ONEX_EXCLUDE: secret_resolver
        "ONEX_SECRET_RESOLVER_CONFIG_PATH", ""
    ).strip()
    if not config_path:
        return None

    path = Path(config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = ModelSecretResolverConfig.model_validate(raw)
    resolver = SecretResolver(config=config)
    store: ProtocolSecretStore = _MappedSecretStore(resolver)
    return store


def clear_secret_store_resolver_cache() -> None:
    """Clear cached lane secret mapping state after config changes in tests."""
    _configured_secret_store.cache_clear()


def _default_secret_store() -> ProtocolSecretStore:
    """Return the default effect-boundary secret store.

    When ``ONEX_SECRET_RESOLVER_CONFIG_PATH`` is set, logical secret refs are
    resolved through the lane secret mapping before reaching the concrete source.
    Otherwise ``AdapterEnvSecretStore`` remains the compatibility backend for
    runtime profiles where a logical resolver is not wired yet.
    """
    configured = _configured_secret_store()
    if configured is not None:
        return configured
    store: ProtocolSecretStore = AdapterEnvSecretStore()
    return store


async def resolve_api_key_async(
    api_key_ref: str | None,
    *,
    store: ProtocolSecretStore | None = None,
    required: bool = True,
) -> SecretStr | None:
    """Resolve the secret VALUE for an ``api_key_ref`` through the secret store.

    Args:
        api_key_ref: The secret reference (key NAME) declared by the routing
            authority, or ``None`` for unauthenticated backends.
        store: The ``ProtocolSecretStore`` to resolve through. Defaults to the
            env-backed store; inject an Infisical-backed store at the deployed
            effect boundary.
        required: When ``True`` (the effect-boundary default), a declared
            reference with no secret-store value fails closed (raises). When
            ``False``, a missing/empty value returns ``None`` — for opt-in
            registration paths where an absent secret simply means "provider
            not configured on this host."

    Returns:
        The resolved secret wrapped in ``SecretStr``, or ``None`` when
        ``api_key_ref`` is ``None`` (unauthenticated backend) or when
        ``required`` is ``False`` and the value is absent.

    Raises:
        SecretResolutionError: When ``required`` is ``True`` and ``api_key_ref``
            is declared but the secret store has no non-empty value for it.
            Fail-closed; no default.
    """
    if not api_key_ref:
        return None

    resolver = store if store is not None else _default_secret_store()
    value = await resolver.get_secret(api_key_ref)
    if not value:
        if not required:
            return None
        raise SecretResolutionError(
            f"Secret reference {api_key_ref!r} declared by the routing authority "
            "could not be resolved from the secret store (missing or empty). "
            "No fallback is permitted."
        )
    return SecretStr(value)


def resolve_api_key(
    api_key_ref: str | None,
    *,
    store: ProtocolSecretStore | None = None,
    required: bool = True,
) -> SecretStr | None:
    """Synchronous wrapper over :func:`resolve_api_key_async`.

    For use at sync effect boundaries (e.g. ``HandlerInferenceIntent.handle``).
    Drives the async secret store via ``asyncio.run``; raises if called from
    within a running event loop (the async variant must be used there).

    Raises:
        SecretResolutionError: When ``required`` is ``True`` and the declared
            reference cannot be resolved.
        RuntimeError: When invoked from inside a running event loop.
    """
    if not api_key_ref:
        return None

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        running_loop = False
    else:
        running_loop = True

    if running_loop:
        raise RuntimeError(
            "resolve_api_key() is sync-only; call resolve_api_key_async() from "
            "an async context."
        )

    return asyncio.run(
        resolve_api_key_async(api_key_ref, store=store, required=required)
    )


def _resolve_api_key_from_running_loop(
    api_key_ref: str,
    *,
    store: ProtocolSecretStore | None,
    required: bool,
) -> SecretStr | None:
    """Resolve from sync code that is already executing inside an event loop."""
    result: Queue[tuple[SecretStr | None, BaseException | None]] = Queue(maxsize=1)

    def _runner() -> None:
        try:
            resolved = asyncio.run(
                resolve_api_key_async(api_key_ref, store=store, required=required)
            )
        except BaseException as exc:
            result.put((None, exc))
        else:
            result.put((resolved, None))

    thread = Thread(
        target=_runner,
        name="omnimarket-secret-availability",
        daemon=True,
    )
    thread.start()
    thread.join()
    resolved, exc = result.get()
    if exc is not None:
        raise exc
    return resolved


def api_key_ref_available(
    api_key_ref: str | None,
    *,
    store: ProtocolSecretStore | None = None,
) -> bool:
    """Return whether a secret ref resolves to a non-empty value.

    This is for routing-time availability checks that must avoid selecting a
    backend the active runtime secret store cannot use. It returns only a
    boolean; the route decision still carries the secret reference name, never
    the secret value.
    """
    if not api_key_ref:
        return True
    try:
        resolved = resolve_api_key(api_key_ref, store=store, required=False)
    except RuntimeError as exc:
        if "sync-only" not in str(exc):
            raise
        resolved = _resolve_api_key_from_running_loop(
            api_key_ref,
            store=store,
            required=False,
        )
    return resolved is not None


__all__: list[str] = [
    "SecretResolutionError",
    "api_key_ref_available",
    "resolve_api_key",
    "resolve_api_key_async",
]
