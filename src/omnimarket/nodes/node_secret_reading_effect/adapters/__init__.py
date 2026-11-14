# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The backends of the secret reading.

Stores: an in-memory test store with scopes, and the secret store effect, which resolves through
``node_secret_store_effect``'s handler, the one resolver the system already runs, so the reading
probes the same path a consumer takes. Surfaces: an in-memory map and the local files. A resolved
value stays inside ``ModelResolvedSecret`` and is never returned in a serializable field.
"""

from __future__ import annotations

import asyncio
import glob
import os
from collections.abc import Callable, Mapping

from pydantic import SecretStr

from omnimarket.models.model_secret_reading import (
    EnumSecretProbeOutcome,
    ModelResolvedSecret,
    ModelSecretHandle,
)
from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)
from omnimarket.models.secret_store.model_secret_store_request import (
    EnumSecretStoreOperation,
    ModelSecretStoreRequest,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
    ModelSecretStoreResult,
)

_STORE_TO_PROBE: dict[EnumSecretStoreOutcome, EnumSecretProbeOutcome] = {
    EnumSecretStoreOutcome.RESOLVED: EnumSecretProbeOutcome.RESOLVED,
    EnumSecretStoreOutcome.NOT_FOUND: EnumSecretProbeOutcome.NOT_FOUND,
    EnumSecretStoreOutcome.REFUSED: EnumSecretProbeOutcome.REFUSED,
    EnumSecretStoreOutcome.UNREACHABLE: EnumSecretProbeOutcome.UNREACHABLE,
    EnumSecretStoreOutcome.NOT_CONFIGURED: EnumSecretProbeOutcome.NOT_CONFIGURED,
    EnumSecretStoreOutcome.MISCONFIGURED: EnumSecretProbeOutcome.MISCONFIGURED,
}


class InMemorySecretProbeStore:
    """A test store with scopes. ``revoke`` takes a consumer's scope away: its probe is refused.

    ``down`` makes every probe unreachable, the dependency-down fault.
    """

    def __init__(self, keys: Mapping[tuple[str, str], str]) -> None:
        self._keys = dict(keys)
        self._revoked: set[str] = set()
        self.down = False

    def revoke(self, consumer: str) -> None:
        self._revoked.add(consumer)

    def resolve(self, handle: ModelSecretHandle) -> ModelResolvedSecret:
        if self.down:
            return ModelResolvedSecret(outcome=EnumSecretProbeOutcome.UNREACHABLE)
        if handle.consumer in self._revoked:
            return ModelResolvedSecret(outcome=EnumSecretProbeOutcome.REFUSED)
        value = self._keys.get((handle.folder, handle.key))
        if value is None:
            return ModelResolvedSecret(outcome=EnumSecretProbeOutcome.NOT_FOUND)
        if not value:
            return ModelResolvedSecret(outcome=EnumSecretProbeOutcome.EMPTY)
        return ModelResolvedSecret(
            outcome=EnumSecretProbeOutcome.RESOLVED, value=SecretStr(value)
        )


SecretResolve = Callable[[ModelSecretStoreRequest], ModelSecretStoreResult]


def resolved_of(result: ModelSecretStoreResult) -> ModelResolvedSecret:
    """The probe's view of one resolve result."""
    if result.outcome is EnumSecretStoreOutcome.RESOLVED:
        if result.value is None or not result.value.get_secret_value():
            return ModelResolvedSecret(outcome=EnumSecretProbeOutcome.EMPTY)
        return ModelResolvedSecret(
            outcome=EnumSecretProbeOutcome.RESOLVED, value=result.value
        )
    return ModelResolvedSecret(
        outcome=_STORE_TO_PROBE.get(result.outcome, EnumSecretProbeOutcome.FAILED)
    )


class SecretStoreEffectProbeStore:
    """Resolve through node_secret_store_effect's handler, as every consumer does."""

    def __init__(self, resolve: SecretResolve) -> None:
        self._resolve = resolve

    @classmethod
    def from_overlay(
        cls, overlay: ModelSecretStoreOverlay
    ) -> SecretStoreEffectProbeStore:
        from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
            HandlerSecretStore,
        )

        handler = HandlerSecretStore(overlay=overlay)

        def resolve(request: ModelSecretStoreRequest) -> ModelSecretStoreResult:
            return asyncio.run(handler.handle(request))

        return cls(resolve)

    def resolve(self, handle: ModelSecretHandle) -> ModelResolvedSecret:
        request = ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.RESOLVE,
            folder=handle.folder,
            key=handle.key,
        )
        try:
            result = self._resolve(request)
        except Exception:
            return ModelResolvedSecret(outcome=EnumSecretProbeOutcome.UNREACHABLE)
        return resolved_of(result)


class InMemoryExposureSurfaces:
    """Surfaces as a map of name to text. A name in ``unreadable`` fails its read."""

    def __init__(self, texts: Mapping[str, str]) -> None:
        self.texts = dict(texts)
        self.unreadable: set[str] = set()

    def paths(self, pattern: str) -> tuple[str, ...]:
        return tuple(sorted(name for name in self.texts if name == pattern))

    def read_text(self, path: str, max_bytes: int) -> str:
        if path in self.unreadable:
            raise OSError(path)
        return self.texts[path][-max_bytes:]


class LocalExposureSurfaces:
    """Local files named by glob patterns, with ``~`` and environment variables expanded."""

    def paths(self, pattern: str) -> tuple[str, ...]:
        expanded = os.path.expandvars(os.path.expanduser(pattern))
        return tuple(
            sorted(p for p in glob.glob(expanded, recursive=True) if os.path.isfile(p))
        )

    def read_text(self, path: str, max_bytes: int) -> str:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            return handle.read().decode("utf-8", errors="replace")


__all__ = [
    "InMemoryExposureSurfaces",
    "InMemorySecretProbeStore",
    "LocalExposureSurfaces",
    "SecretStoreEffectProbeStore",
    "resolved_of",
]
