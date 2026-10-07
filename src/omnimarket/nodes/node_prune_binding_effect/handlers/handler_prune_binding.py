# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve retention bindings at the effect boundary using the runtime overlay."""

from __future__ import annotations

import os
from pathlib import Path

from omnibase_infra.runtime.overlay.errors import (
    OverlayNotFoundError,
    OverlayPermissionError,
    OverlaySchemaInvalidError,
)
from omnibase_infra.runtime.overlay.overlay_file_loader import OverlayFileLoader
from omnibase_spi.protocols.services import ProtocolSecretStore
from pydantic import SecretStr, ValidationError

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.inference.secret_store_resolver import (
    SecretResolutionError,
    SecretStoreConfigurationError,
    _configured_secret_store,
    resolve_api_key_loop_safe,
)
from omnimarket.nodes.node_prune_binding_effect.models import (
    ModelPruneBinding,
    ModelPruneBindingRequest,
    ModelPruneBindingResult,
    PruneKind,
)

PRUNE_BINDING_OVERLAY_ENV = "OMNIMARKET_PRUNE_BINDING_OVERLAY"


class PruneConfigurationError(ValueError):
    """A missing or invalid binding; safe to return without dead-lettering."""


class HandlerPruneBinding:
    """Read the runtime overlay and resolve credentials at the effect boundary."""

    def __init__(self, *, store: ProtocolSecretStore | None = None) -> None:
        self._store = store

    def handle(self, request: ModelPruneBindingRequest) -> ModelPruneBindingResult:
        binding = load_prune_binding(request.binding, request.kind)
        database_url = (
            SecretStr(prune_database_url(binding, request.kind, store=self._store))
            if request.resolve_database_url
            else None
        )
        return ModelPruneBindingResult(binding=binding, database_url=database_url)


def load_prune_binding(
    declared: ModelPruneBinding, kind: PruneKind
) -> ModelPruneBinding:
    """Apply services.prune.<kind>.* from the existing typed runtime overlay.

    The environment supplies only a bootstrap file pointer. Without one, use
    the runtime's normal ~/.omnibase/overlay.yaml. No file at that default
    means no overlay; an explicitly selected missing/invalid file refuses.
    """
    pointer = os.environ.get(PRUNE_BINDING_OVERLAY_ENV, "").strip()
    path = Path(pointer) if pointer else Path.home() / ".omnibase" / "overlay.yaml"
    if not pointer and not path.exists():
        return declared
    try:
        overlay = OverlayFileLoader(require_restricted_permissions=True).load(path)
        prefix = f"{kind}."
        overrides = {
            key.removeprefix(prefix): value
            for key, value in overlay.services.get("prune", {}).items()
            if key.startswith(prefix)
        }
        # An overlay choosing one DB source replaces the contract's source;
        # it cannot retain a stale secret reference alongside a literal URL.
        raw = declared.model_dump()
        if "database_url" in overrides or "database_secret_ref" in overrides:
            raw["database_url"] = raw["database_secret_ref"] = None
        raw.update(overrides)
        return ModelPruneBinding.model_validate(raw)
    except (
        OSError,
        OverlayNotFoundError,
        OverlayPermissionError,
        OverlaySchemaInvalidError,
        ValidationError,
    ) as exc:
        # Loader/validation exception text can contain credentials. Only the
        # configuration key and error class cross the result boundary.
        raise PruneConfigurationError(
            f"{PRUNE_BINDING_OVERLAY_ENV}: invalid prune binding ({type(exc).__name__})"
        ) from None


def prune_database_url(
    binding: ModelPruneBinding,
    kind: PruneKind,
    *,
    store: ProtocolSecretStore | None = None,
) -> str:
    """Resolve the declared DB source through the existing lane secret store."""
    if binding.database_url is not None:
        return binding.database_url.get_secret_value()
    ref = binding.database_secret_ref
    if ref is None:
        raise PruneConfigurationError(
            f"config.{kind}_prune.binding.database_url or database_secret_ref is missing"
        )
    try:
        # Select only a declared lane mapping or stored local credential.
        # The general inference resolver's ambient-env default is deliberately
        # excluded from retention bindings.
        resolved_store = store if store is not None else _configured_secret_store()
        if resolved_store is None:
            resolved_store = LocalByokCredentialStore()
        value = resolve_api_key_loop_safe(ref, store=resolved_store, required=True)
    except (SecretResolutionError, SecretStoreConfigurationError):
        raise PruneConfigurationError(
            f"config.{kind}_prune.binding.database_secret_ref is unresolved"
        ) from None
    if value is None:
        raise PruneConfigurationError(
            f"config.{kind}_prune.binding.database_secret_ref is unresolved"
        )
    return value.get_secret_value()
