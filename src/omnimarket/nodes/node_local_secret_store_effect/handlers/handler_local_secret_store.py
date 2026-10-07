# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Store or delete a local secret, and return the credential events for it.

``onex secret set`` used to store a provider key and tell nothing else, so no
Credentials page could list it. This effect stores the key exactly as before
(the local store holds the value; the delegate path reads it there) and returns
the metadata events the local runtime folds into ``tenant_inference_credentials``:

* a provider key (``llm.<provider>.<field>`` for a catalogue provider) is also
  registered under a freshly minted route ref, ``cred_localinstall_<provider>_<uuid>``.
  That ref is the credential generation id: each set mints a new one, so a
  delete then set of one ref name is a second row, never a resurrected one.
  The set returns ``credential-registered`` for it, with the key's fingerprint
  prefix (``sha256(value)[:8]``) and the set time, and, when it replaces an
  earlier route key for the provider, ``credential-revoked`` for that one first;
* a delete of a provider key returns ``credential-revoked`` for the live route ref;
* any other secret is stored or deleted with no event: it is not a provider key.

No event, result, refusal or log line carries the value.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime

from omnimarket.inference.local_byok_credential_adapter import (
    LOCAL_INSTALL_TENANT_ID,
    LocalByokCredentialStore,
    register_local_byok_credential,
    resolve_local_byok_credential_ref,
    revoke_local_byok_credential,
)
from omnimarket.nodes.node_local_secret_store_effect.models.model_local_secret_request import (
    ModelLocalSecretRequest,
)
from omnimarket.nodes.node_local_secret_store_effect.models.model_local_secret_result import (
    ModelLocalSecretResult,
)
from omnimarket.projection.credential_publisher import (
    ModelCredentialRegisteredEvent,
    ModelCredentialRevokedEvent,
)
from omnimarket.routing.byok_provider_backends import resolve_byok_provider_backend
from omnimarket.routing.local_byok_route import house_provider_slug


class LocalSecretStoreRefusedError(Exception):
    """A request the store refuses. Its text names the reference, never the value."""


def offered_provider(secret_ref: str) -> str | None:
    """The BYOK provider a declared ``llm.<provider>.<field>`` ref names, if offered.

    A declared reference is what tier selection resolves, but the route a
    customer's work may run on carries a tenant-shaped reference, so a provider
    key is also registered under one (OMN-19205). ``None`` for a minted reference
    (already the customer's) and for a provider the catalogue does not offer.
    """
    slug = house_provider_slug(secret_ref)
    if slug is None or resolve_byok_provider_backend(slug) is None:
        return None
    return slug


def fingerprint_prefix(value: str) -> str:
    """The first 8 hex characters of sha256(value): tells keys apart, cannot recover one."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class HandlerLocalSecretStore:
    """Writes or deletes one secret in the local store and returns its events."""

    def __init__(self, *, now: Callable[[], datetime] = _utc_now) -> None:
        self._now = now

    def handle(self, request: ModelLocalSecretRequest) -> ModelLocalSecretResult:
        if request.operation == "set":
            return self._set(request)
        return self._delete(request)

    def _set(self, request: ModelLocalSecretRequest) -> ModelLocalSecretResult:
        ref = request.secret_ref
        store = LocalByokCredentialStore()
        if not request.force and asyncio.run(store.get_secret(ref)) is not None:
            raise LocalSecretStoreRefusedError(
                f"{ref} already has a stored value. Pass --force to replace it. "
                "Refusing by default so a re-run of a setup script cannot "
                "silently swap a working credential for a stale one."
            )
        value = request.value.get_secret_value() if request.value is not None else ""
        if not value:
            raise LocalSecretStoreRefusedError(
                f"no value was supplied for {ref}. Pipe the value in, or run this "
                "on a terminal to be prompted for it. The value is never read "
                "from a command-line argument."
            )
        provider = offered_provider(ref)
        asyncio.run(store.set_secret(ref, value))
        if provider is None:
            return ModelLocalSecretResult(operation="set", secret_ref=ref)

        replaced = resolve_local_byok_credential_ref(provider, db_path=store.db_path)
        route_ref = register_local_byok_credential(
            provider,
            value,
            plan=request.plan,
            model=request.model,
            db_path=store.db_path,
        )
        events: list[ModelCredentialRevokedEvent | ModelCredentialRegisteredEvent] = []
        if replaced is not None and replaced != route_ref:
            events.append(
                ModelCredentialRevokedEvent(
                    tenant_id=LOCAL_INSTALL_TENANT_ID, api_key_ref=replaced
                )
            )
        metadata = {
            key: item
            for key, item in (("plan", request.plan), ("model", request.model))
            if item is not None
        }
        events.append(
            ModelCredentialRegisteredEvent(
                tenant_id=LOCAL_INSTALL_TENANT_ID,
                provider=provider,
                name=ref,
                api_key_ref=route_ref,
                metadata=metadata,
                fingerprint=fingerprint_prefix(value),
                set_at=self._now(),
            )
        )
        return ModelLocalSecretResult(
            operation="set",
            secret_ref=ref,
            provider=provider,
            route_ref=route_ref,
            events=tuple(events),
        )

    def _delete(self, request: ModelLocalSecretRequest) -> ModelLocalSecretResult:
        ref = request.secret_ref
        store = LocalByokCredentialStore()
        provider = offered_provider(ref)
        live = (
            resolve_local_byok_credential_ref(provider, db_path=store.db_path)
            if provider is not None
            else None
        )
        if not asyncio.run(store.delete_secret(ref)):
            raise LocalSecretStoreRefusedError(
                f"this machine holds no value for {ref}; nothing was removed. "
                "Run 'onex secret list' to see what is stored."
            )
        if provider is None:
            return ModelLocalSecretResult(operation="delete", secret_ref=ref)
        withdrawn = revoke_local_byok_credential(provider, db_path=store.db_path) > 0
        events = (
            (
                ModelCredentialRevokedEvent(
                    tenant_id=LOCAL_INSTALL_TENANT_ID, api_key_ref=live
                ),
            )
            if live is not None
            else ()
        )
        return ModelLocalSecretResult(
            operation="delete",
            secret_ref=ref,
            provider=provider,
            route_withdrawn=withdrawn,
            events=events,
        )


__all__ = [
    "HandlerLocalSecretStore",
    "LocalSecretStoreRefusedError",
    "fingerprint_prefix",
    "offered_provider",
]
