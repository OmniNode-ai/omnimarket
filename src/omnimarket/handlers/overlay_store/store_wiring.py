# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one wiring function of a process kind: bootstrap inputs in, the injected overlay store out.

It reads the kind first, requires only the inputs that kind declares, fetches only the secrets that kind declares and
builds the store. A missing kind or input refuses with its name before anything is fetched or built. There is no
default kind, address, port or prefix here: every value arrives in ``bootstrap``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from omnibase_spi.protocols.storage import ProtocolOverlayStore

from omnimarket.handlers.overlay_store.store_backend_registry import (
    ProtocolStoreBackend,
    StoreWiringRefusedError,
    load_registered_backends,
)

KIND_INPUT = "ONEX_SETTINGS_STORE_KIND"

SecretFetcher = Callable[[tuple[str, ...]], Mapping[str, str]]


def build_overlay_store(
    bootstrap: Mapping[str, str],
    *,
    backends: Mapping[str, ProtocolStoreBackend] | None = None,
    fetch_secrets: SecretFetcher | None = None,
) -> ProtocolOverlayStore:
    """Build the store the bootstrap inputs name. Refuses, naming the input, and builds nothing when one is missing."""
    registry = load_registered_backends() if backends is None else backends
    kind = _present(bootstrap, KIND_INPUT)
    backend = registry.get(kind)
    if backend is None:
        raise StoreWiringRefusedError(
            f"{KIND_INPUT} names the kind {kind}, which no backend registers "
            f"(registered: {', '.join(sorted(registry)) or 'none'})"
        )
    spec = backend.spec
    inputs = {name: _present(bootstrap, name) for name in spec.inputs}
    secrets: Mapping[str, str] = {}
    if spec.secrets:
        if fetch_secrets is None:
            raise StoreWiringRefusedError(
                f"the kind {kind} needs {', '.join(spec.secrets)} from Infisical and no fetcher was given"
            )
        secrets = fetch_secrets(spec.secrets)
        for name in spec.secrets:
            if not secrets.get(name):
                raise StoreWiringRefusedError(f"Infisical returned no value for {name}")
        secrets = {name: secrets[name] for name in spec.secrets}
    return backend.build(inputs, secrets)


async def wire_overlay_store(
    container: Any,
    bootstrap: Mapping[str, str],
    *,
    backends: Mapping[str, ProtocolStoreBackend] | None = None,
    fetch_secrets: SecretFetcher | None = None,
) -> ProtocolOverlayStore:
    """Build the store and register it in the container under the protocol, for ``get_service``."""
    store = build_overlay_store(
        bootstrap, backends=backends, fetch_secrets=fetch_secrets
    )
    await container.service_registry.register_instance(
        interface=ProtocolOverlayStore,
        instance=store,
    )
    return store


def _present(bootstrap: Mapping[str, str], name: str) -> str:
    value = bootstrap.get(name, "").strip()
    if not value:
        raise StoreWiringRefusedError(f"the bootstrap input {name} is missing")
    return value


__all__ = ["KIND_INPUT", "SecretFetcher", "build_overlay_store", "wire_overlay_store"]
