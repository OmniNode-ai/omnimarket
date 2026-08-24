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


# OMN-13960: provider-native env-var aliases for delegation secret refs.
# ~/.omnibase/.env carries PROVIDER-NATIVE secret names (``OPEN_ROUTER_API_KEY``,
# ``GEMINI_API_KEY``) that do NOT match the dotted-ref → ``LLM_*_API_KEY``
# convention (``llm.openrouter.api_key`` → ``LLM_OPENROUTER_API_KEY``). OMN-13943
# threaded these as a per-CALL-SITE ``env_var_fallback`` (each backend's bifrost
# ``api_key_env``); this map accepts the SAME provider-native names at the STORE
# level so resolution succeeds regardless of whether a given call site remembered
# to thread ``api_key_env`` — the LLM-judge inference adapter did not, so an
# OpenRouter/Gemini-backed judge would fail-closed even though the key is present.
# The CANONICAL source of these names is ``bifrost_delegation.yaml`` ``api_key_env``;
# this store-level map is the resolver-side safety net, not a second source of
# truth. Fail-closed is preserved: this is the LAST lookup, so a genuinely-absent
# secret still resolves to ``None`` (and ``required=True`` callers still raise).
_PROVIDER_NATIVE_SECRET_ALIASES: dict[str, tuple[str, ...]] = {
    "llm.openrouter.api_key": ("OPEN_ROUTER_API_KEY",),
    "llm.gemini.api_key": ("GEMINI_API_KEY",),
}


class _ConventionFallbackSecretStore:
    """Default local store: literal env lookup, then dotted-ref → ENV_VAR convention.

    OMN-13861: the bare ``AdapterEnvSecretStore`` resolves ``os.environ[key]``
    LITERALLY, so a dotted logical ref (``llm.glm.api_key``) never matched the
    canonical ``LLM_GLM_API_KEY`` env var and every authenticated cloud delegation
    call failed to resolve its key on the bus-less local path (no
    ``ONEX_SECRET_RESOLVER_CONFIG_PATH`` lane wired). This store composes both
    lookups and is a STRICT SUPERSET of the prior literal-only behavior:

      1. literal ``AdapterEnvSecretStore`` — a caller (or test fixture) that sets
         the exact ref name (incl. a literal dotted ``llm.glm.api_key``) still
         resolves, so nothing that worked before breaks;
      2. on a miss, the ``SecretResolver`` convention fallback maps the dotted ref
         to its canonical env-var name (``llm.glm.api_key`` → ``LLM_GLM_API_KEY``),
         so the real ``LLM_*_API_KEY`` env vars now resolve.

    A ref that is already an env-var-shaped name resolves identically in both
    lookups. Read-only, like the sibling stores.
    """

    def __init__(self) -> None:
        self._literal: ProtocolSecretStore = AdapterEnvSecretStore()
        self._convention: ProtocolSecretStore = _MappedSecretStore(
            SecretResolver(
                config=ModelSecretResolverConfig(enable_convention_fallback=True)
            )
        )

    async def get_secret(self, key: str) -> str | None:
        literal = await self._literal.get_secret(key)
        if literal:
            return literal
        convention = await self._convention.get_secret(key)
        if convention:
            return convention
        # OMN-13960: provider-native alias — accept the real ~/.omnibase/.env name
        # (e.g. ``OPEN_ROUTER_API_KEY``/``GEMINI_API_KEY``) when neither the literal
        # ref nor the dotted → ``LLM_*_API_KEY`` convention resolved. This makes
        # OpenRouter/Gemini secrets resolvable from ANY call site, including ones
        # that do not thread the bifrost ``api_key_env`` fallback (the judge path).
        for alias in _PROVIDER_NATIVE_SECRET_ALIASES.get(key, ()):
            value = os.environ.get(alias)
            if value:
                return value
        return None

    async def set_secret(self, key: str, value: str) -> bool:
        raise RuntimeError("Convention-fallback secret store is read-only")

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("Convention-fallback secret store is read-only")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        return await self._literal.list_keys(prefix)

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
    resolved through the explicit lane secret mapping before reaching the concrete
    source (that path is authoritative and unchanged).

    Otherwise (OMN-13861) the default store is a ``_ConventionFallbackSecretStore``
    (literal env lookup, then dotted-ref → ENV_VAR convention), NOT a bare
    ``AdapterEnvSecretStore``. The routing authority carries dotted logical
    ``secret_ref``s (e.g. ``llm.glm.api_key``); the bare env adapter did a LITERAL
    ``os.environ.get("llm.glm.api_key")`` that never matched the canonical
    ``LLM_*_API_KEY`` env vars, so every authenticated cloud delegation call failed
    to resolve its key on the bus-less local path when no
    ``ONEX_SECRET_RESOLVER_CONFIG_PATH`` lane was wired. The composite store adds the
    convention mapping (``llm.glm.api_key`` → ``LLM_GLM_API_KEY``) as a strict
    SUPERSET of the prior literal behavior, so both a literal ref and the canonical
    env-var form resolve. An explicit ``ONEX_SECRET_RESOLVER_CONFIG_PATH``
    (Infisical/lane mapping) still overrides it above.
    """
    configured = _configured_secret_store()
    if configured is not None:
        return configured
    store: ProtocolSecretStore = _ConventionFallbackSecretStore()
    return store


async def resolve_api_key_async(
    api_key_ref: str | None,
    *,
    store: ProtocolSecretStore | None = None,
    required: bool = True,
    env_var_fallback: str | None = None,
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
        env_var_fallback: OMN-13943. An additional literal env-var NAME to
            check when the primary ``api_key_ref`` lookup misses. This is
            distinct from the dotted ``secret_ref`` convention (which maps
            through ``SecretResolver``'s ``LLM_*_API_KEY`` naming): it is the
            backend's own contract-declared ``api_key_env`` (e.g.
            ``GEMINI_API_KEY``, ``OPEN_ROUTER_API_KEY``) — the canonical env
            var already defined in ``~/.omnibase/.env``. The caller always
            supplies this from config data, never a hardcoded literal in this
            module, so no provider-specific alias lives in code here.

    Returns:
        The resolved secret wrapped in ``SecretStr``, or ``None`` when
        ``api_key_ref`` is ``None`` (unauthenticated backend) or when
        ``required`` is ``False`` and the value is absent.

    Raises:
        SecretResolutionError: When ``required`` is ``True`` and ``api_key_ref``
            is declared but neither the secret store nor ``env_var_fallback``
            resolve a non-empty value. Fail-closed; no default.
    """
    if not api_key_ref:
        return None

    resolver = store if store is not None else _default_secret_store()
    value = await resolver.get_secret(api_key_ref)
    if not value and env_var_fallback:
        value = os.environ.get(env_var_fallback) or None
    if not value:
        if not required:
            return None
        fallback_note = (
            f" nor did the declared fallback env var {env_var_fallback!r}"
            if env_var_fallback
            else ""
        )
        raise SecretResolutionError(
            f"Secret reference {api_key_ref!r} declared by the routing authority "
            f"could not be resolved from the secret store (missing or empty){fallback_note}. "
            "No further fallback is permitted."
        )
    return SecretStr(value)


def resolve_api_key(
    api_key_ref: str | None,
    *,
    store: ProtocolSecretStore | None = None,
    required: bool = True,
    env_var_fallback: str | None = None,
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
        resolve_api_key_async(
            api_key_ref,
            store=store,
            required=required,
            env_var_fallback=env_var_fallback,
        )
    )


def _resolve_api_key_from_running_loop(
    api_key_ref: str,
    *,
    store: ProtocolSecretStore | None,
    required: bool,
    env_var_fallback: str | None = None,
) -> SecretStr | None:
    """Resolve from sync code that is already executing inside an event loop."""
    result: Queue[tuple[SecretStr | None, BaseException | None]] = Queue(maxsize=1)

    def _runner() -> None:
        try:
            resolved = asyncio.run(
                resolve_api_key_async(
                    api_key_ref,
                    store=store,
                    required=required,
                    env_var_fallback=env_var_fallback,
                )
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


def resolve_api_key_loop_safe(
    api_key_ref: str | None,
    *,
    store: ProtocolSecretStore | None = None,
    required: bool = True,
    env_var_fallback: str | None = None,
) -> SecretStr | None:
    """Resolve a secret VALUE from SYNC code that may run inside an event loop.

    Identical to :func:`resolve_api_key`, except that when it is invoked from
    within a running event loop the resolution is offloaded to a worker thread
    (via :func:`_resolve_api_key_from_running_loop`) instead of raising the
    "sync-only" guard.

    Use this from a SYNC ``handle()`` that the ONEX runtime may dispatch on the
    event loop: ``LocalRuntimeBusAdapter`` calls a sync handler directly inside
    its async ``on_message``, so a bare :func:`resolve_api_key` would raise
    ``RuntimeError("resolve_api_key() is sync-only ...")`` (OMN-13843). Async
    handlers should keep awaiting :func:`resolve_api_key_async` directly; this
    helper exists only for handlers that must stay synchronous (e.g. because a
    sync orchestrator port calls them in-process).

    Fail-closed semantics are unchanged: with ``required=True`` a declared ref
    with no secret-store value (and no resolvable ``env_var_fallback``) raises
    :class:`SecretResolutionError`.
    """
    if not api_key_ref:
        return None
    try:
        return resolve_api_key(
            api_key_ref,
            store=store,
            required=required,
            env_var_fallback=env_var_fallback,
        )
    except RuntimeError as exc:
        if "sync-only" not in str(exc):
            raise
        return _resolve_api_key_from_running_loop(
            api_key_ref,
            store=store,
            required=required,
            env_var_fallback=env_var_fallback,
        )


async def resolve_tenant_scoped_api_key_async(
    api_key_ref: str | None,
    *,
    tenant_id: str,
    store: ProtocolSecretStore | None = None,
) -> SecretStr | None:
    """Resolve a TENANT-OVERLAY backend's secret VALUE — fail-fast, no house fallback.

    OMN-15631 v1(a). A backend resolved from a tenant's
    ``delegation_routing_tenant_overlay`` row must never silently fall back to
    an OmniNode house key when its own ``secret_ref`` is missing or unresolved
    — that would route a tenant's traffic (and cost) through the platform's
    own provider account without either party's knowledge. This wrapper is a
    thin, INTENTIONALLY-narrower call of :func:`resolve_api_key_async`:
    ``required`` is always ``True`` (fail-fast on a miss) and
    ``env_var_fallback`` is always omitted — the house ``api_key_env``
    convention (``BifrostBackendRef.api_key_env`` / the bifrost contract's
    ``api_key_env`` field) has no tenant-overlay equivalent by construction
    (migration 0001 carries no ``api_key_env`` column), so there is nothing to
    thread here even by accident.

    Args:
        api_key_ref: The tenant-scoped secret reference name, or ``None`` for
            an explicitly unauthenticated tenant backend.
        tenant_id: The tenant this resolution is scoped to — carried for
            structured logging/error attribution only; the actual store
            lookup key is still ``api_key_ref`` (the store is not itself
            tenant-partitioned in v1(a) — see OMN-13236's ``ProtocolSecretStore``
            contract, which this reuses unchanged).
        store: The ``ProtocolSecretStore`` to resolve through. Defaults to the
            same env-backed store :func:`resolve_api_key_async` uses.

    Returns:
        The resolved secret wrapped in ``SecretStr``, or ``None`` only when
        ``api_key_ref`` is ``None`` (an explicitly unauthenticated backend).

    Raises:
        SecretResolutionError: When ``api_key_ref`` is declared but the
            secret store has no value for it. No further fallback exists.
    """
    if not api_key_ref:
        return None
    try:
        return await resolve_api_key_async(
            api_key_ref,
            store=store,
            required=True,
            env_var_fallback=None,
        )
    except SecretResolutionError as exc:
        raise SecretResolutionError(
            f"Tenant {tenant_id!r} overlay backend declared secret_ref "
            f"{api_key_ref!r} which could not be resolved from the secret "
            "store. Tenant-overlay backends never fall back to a house key — "
            "the tenant must register this ref in the secret store before "
            "this backend is routable."
        ) from exc


def api_key_ref_available(
    api_key_ref: str | None,
    *,
    store: ProtocolSecretStore | None = None,
    env_var_fallback: str | None = None,
) -> bool:
    """Return whether a secret ref resolves to a non-empty value.

    This is for routing-time availability checks that must avoid selecting a
    backend the active runtime secret store cannot use. It returns only a
    boolean; the route decision still carries the secret reference name, never
    the secret value.

    ``env_var_fallback`` (OMN-13943): when supplied, a backend whose dotted
    ``secret_ref`` convention mapping misses but whose own contract-declared
    literal env var IS set is still reported available — the routing tier
    eligibility check must agree with what the effect boundary will actually
    resolve at call time (:func:`resolve_api_key_async`), or a tier could be
    reported unroutable while its backend is actually callable.
    """
    if not api_key_ref:
        return True
    resolved = resolve_api_key_loop_safe(
        api_key_ref,
        store=store,
        required=False,
        env_var_fallback=env_var_fallback,
    )
    return resolved is not None


__all__: list[str] = [
    "SecretResolutionError",
    "api_key_ref_available",
    "resolve_api_key",
    "resolve_api_key_async",
    "resolve_api_key_loop_safe",
    "resolve_tenant_scoped_api_key_async",
]
