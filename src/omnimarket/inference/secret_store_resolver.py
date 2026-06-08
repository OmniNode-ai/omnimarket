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

from omnibase_infra.secret_stores import AdapterEnvSecretStore
from omnibase_spi.protocols.services import ProtocolSecretStore
from pydantic import SecretStr


class SecretResolutionError(RuntimeError):
    """Raised when a declared ``api_key_ref`` cannot be resolved fail-closed."""


def _default_secret_store() -> ProtocolSecretStore:
    """Return the default effect-boundary secret store.

    ``AdapterEnvSecretStore`` reads from ``os.environ`` and is the canonical
    ``ProtocolSecretStore`` for runtime profiles where Infisical is not wired.
    A deployed Infisical-backed store is injected via ``store=`` instead.
    """
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


__all__: list[str] = [
    "SecretResolutionError",
    "resolve_api_key",
    "resolve_api_key_async",
]
