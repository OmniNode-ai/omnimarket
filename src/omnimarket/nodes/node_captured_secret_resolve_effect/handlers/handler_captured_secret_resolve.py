# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve a captured secret reference through the secret store (OMN-20926).

Operator ruling 2026-10-10 (RULING row 2026-10-10T22:38:48Z
lane=secret-refs-9f8a): when the content scrubber finds a secret in captured
content, the secret is stored in the secret store and the record carries a
reference in its place. Anything that needs the value resolves the reference
through the store, which checks permission and logs access. Ciphertext never
goes on the bus.

The reference shape, the store key it names and the regex group that holds the
secret all come from the capture-redaction contract's ``secret_reference``
block. This module holds none of them.

Who may read: the store decides. The handler reads through the
``ProtocolSecretStore`` it was built with, scoped to the captured namespace and
authenticated as the CALLER's identity (:func:`build_captured_secret_store`).

What is logged: one line per resolution naming the reference, the accessor and
the outcome. Never the value. The store keeps its own audit of the identity.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Protocol

from pydantic import SecretStr

from omnimarket.models.captured_secret.model_captured_secret_store_overlay import (
    CAPTURED_SECRET_STORE_BLOCK,
    ModelCapturedSecretStoreOverlay,
    load_captured_secret_store_overlay,
)
from omnimarket.nodes.node_captured_secret_resolve_effect.models.model_captured_secret_resolve_request import (
    ModelCapturedSecretResolveRequest,
)
from omnimarket.nodes.node_captured_secret_resolve_effect.models.model_captured_secret_resolve_result import (
    EnumCapturedSecretResolveOutcome,
    ModelCapturedSecretResolveResult,
)
from omnimarket.nodes.node_event_emit_effect.redaction import (
    SecretReference,
    load_contract,
)

logger = logging.getLogger(__name__)

_DIGEST_LENGTH = 64


class CapturedSecretReferenceError(ValueError):
    """The text is not one captured secret reference the contract declares."""


class ProtocolCapturedSecretReader(Protocol):
    """The read slice of ``ProtocolSecretStore`` this handler may use.

    No ``set_secret``: a resolver that cannot write cannot overwrite a stored
    secret by mistake. ``InfisicalSecretStore`` satisfies this structurally.
    """

    async def get_secret(self, key: str) -> str | None: ...


def _contract_reference(contract_path: Path | None) -> SecretReference:
    reference = load_contract(contract_path).secret_reference
    if reference is None:
        raise CapturedSecretReferenceError(
            "the capture-redaction contract declares no secret_reference"
        )
    return reference


def store_key_for_reference(text: str, *, contract_path: Path | None = None) -> str:
    """The store key one whole reference names, or raise.

    The whole text must be one reference; a reference inside other text is
    found with :func:`find_captured_secret_references` first.
    """
    reference = _contract_reference(contract_path)
    if reference.pattern.fullmatch(text) is None:
        raise CapturedSecretReferenceError(
            "not a captured secret reference: the contract's secret_reference "
            "pattern does not match the whole text"
        )
    prefix, _, suffix = reference.template.partition("{digest}")
    digest = text[len(prefix) : len(text) - len(suffix)]
    if len(digest) != _DIGEST_LENGTH or reference.reference_for(digest) != text:
        raise CapturedSecretReferenceError(
            "the reference does not round-trip through the contract template"
        )
    return reference.store_key_for(digest)


def find_captured_secret_references(
    text: str, *, contract_path: Path | None = None
) -> list[str]:
    """Every reference in ``text``, in order, duplicates kept."""
    reference = _contract_reference(contract_path)
    return [m.group(0) for m in reference.pattern.finditer(text)]


class HandlerCapturedSecretResolve:
    """``handle(request) -> result``: one reference, one store read, one log line."""

    def __init__(
        self,
        *,
        store: ProtocolCapturedSecretReader,
        contract_path: Path | None = None,
    ) -> None:
        self._store = store
        self._contract_path = contract_path

    def handle(
        self, request: ModelCapturedSecretResolveRequest
    ) -> ModelCapturedSecretResolveResult:
        """Resolve synchronously. From async code, await :meth:`handle_async`."""
        return asyncio.run(self.handle_async(request))

    async def handle_async(
        self, request: ModelCapturedSecretResolveRequest
    ) -> ModelCapturedSecretResolveResult:
        try:
            store_key = store_key_for_reference(
                request.reference, contract_path=self._contract_path
            )
        except CapturedSecretReferenceError:
            return self._outcome(request, EnumCapturedSecretResolveOutcome.MALFORMED)
        try:
            value = await self._store.get_secret(store_key)
        except Exception as exc:  # any store failure is a refusal, fail-closed
            return self._outcome(
                request,
                EnumCapturedSecretResolveOutcome.REFUSED,
                store_key=store_key,
                detail=type(exc).__name__,
            )
        if not value:
            return self._outcome(
                request, EnumCapturedSecretResolveOutcome.MISSING, store_key=store_key
            )
        return self._outcome(
            request,
            EnumCapturedSecretResolveOutcome.RESOLVED,
            store_key=store_key,
            value=SecretStr(value),
        )

    def _outcome(
        self,
        request: ModelCapturedSecretResolveRequest,
        outcome: EnumCapturedSecretResolveOutcome,
        *,
        store_key: str | None = None,
        value: SecretStr | None = None,
        detail: str | None = None,
    ) -> ModelCapturedSecretResolveResult:
        level = (
            logging.INFO
            if outcome is EnumCapturedSecretResolveOutcome.RESOLVED
            else logging.WARNING
        )
        logger.log(
            level,
            "captured secret reference %s ref=%s accessor=%s detail=%s",
            outcome.value,
            request.reference
            if outcome is not EnumCapturedSecretResolveOutcome.MALFORMED
            else "<malformed>",
            request.accessor,
            detail or "-",
        )
        return ModelCapturedSecretResolveResult(
            reference=request.reference,
            accessor=request.accessor,
            outcome=outcome,
            store_key=store_key,
            value=value,
            detail=detail,
        )


def build_captured_secret_store(
    overlay: ModelCapturedSecretStoreOverlay,
    *,
    client_id: SecretStr,
    client_secret: SecretStr,
) -> ProtocolCapturedSecretReader:
    """An initialized store for the captured namespace, as the caller's identity.

    The identity is passed in, never read here, so this node reads no
    environment. The caller owns the returned store and closes it.
    """
    from omnibase_infra.adapters._internal import adapter_infisical
    from omnibase_infra.adapters.models.model_infisical_config import (
        ModelInfisicalAdapterConfig,
    )
    from omnibase_infra.secret_stores.infisical_secret_store import (
        InfisicalSecretStore,
    )

    config = ModelInfisicalAdapterConfig(
        host=overlay.infisical_addr,
        client_id=client_id,
        client_secret=client_secret,
        project_id=overlay.project_id,
        environment_slug=overlay.environment_slug,
        secret_path=overlay.secret_path,
    )
    adapter = adapter_infisical.AdapterInfisical(config)
    adapter.initialize()
    return InfisicalSecretStore(
        adapter,
        project_id=str(config.project_id),
        environment_slug=config.environment_slug,
        secret_path=config.secret_path,
    )


class CapturedSecretReaderNotConfiguredError(ValueError):
    """This machine's overlay names no reader identity for the captured store."""


class _ThisMachineReader:
    """The reader identity's store, logged in on the first read and not before.

    A malformed reference is answered by the handler without a read, so it
    never costs a login. The client secret is held as a ``SecretStr``.
    """

    def __init__(
        self,
        overlay: ModelCapturedSecretStoreOverlay,
        *,
        client_id: SecretStr,
        client_secret: SecretStr,
    ) -> None:
        self._overlay = overlay
        self._client_id = client_id
        self._client_secret = client_secret
        self._store: ProtocolCapturedSecretReader | None = None

    async def get_secret(self, key: str) -> str | None:
        if self._store is None:
            self._store = await asyncio.to_thread(
                build_captured_secret_store,
                self._overlay,
                client_id=self._client_id,
                client_secret=self._client_secret,
            )
        return await self._store.get_secret(key)


def reader_for_this_machine(onex_home: Path) -> ProtocolCapturedSecretReader:
    """The READER identity this machine's overlay names, as a lazy store.

    The overlay is ``<onex_home>/config.yaml`` block ``captured_secret_store``;
    the reader's client secret is read from ``<onex_home>/credentials.json``,
    refused unless it is mode 0600. Configuration faults raise here, before any
    reference is looked at, with the file and the key named (never a value).

    Raises:
        CapturedSecretReaderNotConfiguredError: no block, or no reader identity.
    """
    from omnibase_infra.cli.store_onex_home_files import StoreOnexHomeFiles

    files = StoreOnexHomeFiles(onex_home)
    try:
        overlay = load_captured_secret_store_overlay(
            files.load_config(must_exist=False), source=str(files.config_path)
        )
    except ValueError as exc:
        raise CapturedSecretReaderNotConfiguredError(str(exc)) from None
    if overlay.reader_client_id is None or overlay.reader_client_secret_ref is None:
        raise CapturedSecretReaderNotConfiguredError(
            f"{files.config_path} block '{CAPTURED_SECRET_STORE_BLOCK}' names no "
            "reader identity (reader_client_id, reader_client_secret_ref)"
        )
    remediation = (
        "file the reader identity's client secret in credentials.json under "
        f"'{overlay.reader_client_secret_ref}' (mode 0600)"
    )
    return _ThisMachineReader(
        overlay,
        client_id=SecretStr(overlay.reader_client_id),
        client_secret=SecretStr(
            files.read_secret(overlay.reader_client_secret_ref, remediation)
        ),
    )


__all__ = [
    "CapturedSecretReaderNotConfiguredError",
    "CapturedSecretReferenceError",
    "HandlerCapturedSecretResolve",
    "ProtocolCapturedSecretReader",
    "build_captured_secret_store",
    "find_captured_secret_references",
    "reader_for_this_machine",
    "store_key_for_reference",
]
