# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lab host resolves a declared provider ref from the secret store first (OMN-19998).

``llm.openrouter.api_key`` resolved from the host-local store alone (OMN-18695), so every lab host
needed a hand-copied local entry and a host without one failed the ``delegation-key`` parity probe.
The secret store is the source. A host with its machine identity (the process environment, or the
five bootstrap names in ``~/.omnibase/.env``) asks the store first for the refs the packaged
``host_secret_sources.v1.yaml`` map holds; its local entry is only a cache, answering a ref the store
does not hold or cannot be reached for. A host without an identity, and a ref the map does not hold,
resolve as before, and a provider ref never falls back to the environment.

Names and paths only: no value is read, held or logged here except the one the store returns to the
caller, which stays behind the store's own protocol.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Final

import yaml
from omnibase_infra.runtime.models.model_secret_resolver_config import (
    ModelSecretResolverConfig,
)
from omnibase_spi.protocols.services import ProtocolSecretStore

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore

_LOG = logging.getLogger(__name__)

#: The Infisical machine identity a process needs: the store address, the identity, and where in the store.
INFISICAL_BOOTSTRAP_VARS: Final[tuple[str, ...]] = (
    "INFISICAL_ADDR",
    "INFISICAL_CLIENT_ID",
    "INFISICAL_CLIENT_SECRET",
    "INFISICAL_PROJECT_ID",
    "INFISICAL_ENVIRONMENT_SLUG",
)

#: Where a lab host keeps that identity, relative to the user's home: the one intake .env path every host
#: uses (``store_identity_env_file`` in the remote_lane overlay). It holds the bootstrap names and nothing
#: else, so a process with no environment of its own (the parity probe, an ``onex`` call from a plain
#: ssh) still reaches the store the lane reads from.
HOST_BOOTSTRAP_ENV_FILE: Final[str] = ".omnibase/.env"

#: The packaged map of declared provider refs to the store path that holds each one.
HOST_SECRET_SOURCES_FILE: Final[str] = "host_secret_sources.v1.yaml"


class HostSecretSourcesError(RuntimeError):
    """The packaged host secret sources map is malformed."""


def _host_bootstrap_file_values() -> dict[str, str]:
    """The bootstrap names the host file holds. Only the five names are read; a missing file is empty."""
    try:
        text = (Path.home() / HOST_BOOTSTRAP_ENV_FILE).read_text(encoding="utf-8")
    except OSError:
        return {}
    found: dict[str, str] = {}
    for line in text.splitlines():
        name, sep, raw = line.partition("=")
        name = name.strip()
        if sep and name in INFISICAL_BOOTSTRAP_VARS:
            found[name] = raw.strip().strip("\"'")
    return found


def bootstrap_values(environ: Mapping[str, str]) -> dict[str, str]:
    """Each bootstrap name's value: the given environment wins, the host file fills the rest."""
    values = {name: environ.get(name, "").strip() for name in INFISICAL_BOOTSTRAP_VARS}
    if all(values.values()):
        return values
    from_file = _host_bootstrap_file_values()
    return {name: value or from_file.get(name, "") for name, value in values.items()}


@lru_cache(maxsize=1)
def host_provider_sources() -> dict[str, str]:
    """Declared provider ref to the store path that holds it, from the packaged map."""
    raw = yaml.safe_load(
        (files("omnimarket.configs") / HOST_SECRET_SOURCES_FILE).read_text("utf-8")
    )
    sources = raw.get("sources") if isinstance(raw, dict) else None
    if not isinstance(sources, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in sources.items()
    ):
        raise HostSecretSourcesError(
            f"{HOST_SECRET_SOURCES_FILE} must hold a 'sources' map of ref to store path"
        )
    return dict(sources)


@lru_cache(maxsize=1)
def _host_provider_store() -> ProtocolSecretStore:
    """A read-only store over the packaged map, built on first read from the host's machine identity."""
    from omnimarket.inference.secret_store_resolver import (
        secret_store_from_resolver_config,
    )

    return secret_store_from_resolver_config(
        ModelSecretResolverConfig.model_validate(
            {
                "enable_convention_fallback": False,
                "mappings": [
                    {
                        "logical_name": ref,
                        "source": {"source_type": "infisical", "source_path": path},
                    }
                    for ref, path in host_provider_sources().items()
                ],
            }
        )
    )


class StoreThenLocalStore:
    """Answer from the secret store; the local entry answers only a ref the store lacks or cannot serve.

    The local entry is a cache of the store, never the source of truth.
    """

    def __init__(
        self, primary: ProtocolSecretStore, cache: ProtocolSecretStore
    ) -> None:
        self._primary = primary
        self._cache = cache

    async def get_secret(self, key: str) -> str | None:
        try:
            value = await self._primary.get_secret(key)
        except (
            Exception
        ) as exc:  # store unreachable: the cache answers; only the type is logged
            _LOG.warning(
                "secret store read failed (%s); using the local cache",
                type(exc).__name__,
            )
            value = None
        return value or await self._cache.get_secret(key)

    async def set_secret(self, key: str, value: str) -> bool:
        raise RuntimeError("Store-then-local secret store is read-only")

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("Store-then-local secret store is read-only")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        del prefix
        return []

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds


def host_store_for(
    api_key_ref: str, environ: Mapping[str, str]
) -> ProtocolSecretStore | None:
    """The store-first answerer for a declared provider ref the packaged map holds, else ``None``.

    ``None`` on a machine without its identity, so it resolves from the local store alone as before.
    """
    if api_key_ref not in host_provider_sources():
        return None
    if not all(bootstrap_values(environ).values()):
        return None
    return StoreThenLocalStore(_host_provider_store(), LocalByokCredentialStore())


def clear_host_provider_cache() -> None:
    """Clear the cached map and store after a change in tests."""
    host_provider_sources.cache_clear()
    _host_provider_store.cache_clear()
