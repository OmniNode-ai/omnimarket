# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Store a secret found in captured content, and hand back its reference (OMN-20926).

Operator ruling 2026-10-10 (RULING row 2026-10-10T22:38:48Z,
lane=secret-refs-9f8a): when the content scrubber finds a secret in captured
content, the secret is stored in the secret store and the record carries a
reference in its place. Anything that needs the value resolves the reference
through the store, which checks permission and logs access. Ciphertext never
goes on the bus.

Where a secret lands
--------------------
One key per distinct value: the capture-redaction contract's
``store_key_template`` filled with the value's digest, in the folder the
machine's private overlay names. The digest is HMAC-SHA256 of the value under a
per-deployment reference key, so the same value always lands on the same key
(deduplicated) and the bus never carries an unsalted hash of a low-entropy
secret. The secret's comment carries the capturing session and the time.

The write is CREATE ONLY and goes through the platform's one secret store
adapter, node_secret_store_effect (OMN-20948): this handler builds that node's
request and reads its typed outcome, and owns no store client of its own. An
existing key is the dedupe case and is accepted without reading or updating
it, so the writer identity needs neither a read nor an edit right on the
namespace: a compromised writer cannot read back what it stored. The capturing
session is not written beside the secret: the captured record on the bus
carries the session and the reference together, and the store's own audit log
records the writing identity and the time.

Configuration
-------------
``<onex_home>/config.yaml`` block ``captured_secret_store`` (the store
provider and addresses, the writer's client id and two references) with the
values in
``<onex_home>/credentials.json``, refused unless mode 0600: the split
``onex auth login`` already uses. Nothing deployment specific is committed.

Failure
-------
Every failure is a typed outcome with no reference, and the first one is
sticky for the handler's life: one unreachable store costs one timeout per
capture, not one per secret. Nothing here logs or returns a value, a digest or
a credential.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import hmac
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnibase_spi.protocols.services import ProtocolSecretStore
from pydantic import SecretStr

from omnimarket.models.captured_secret.model_captured_secret_store_overlay import (
    CAPTURED_SECRET_STORE_BLOCK,
    load_captured_secret_store_overlay,
)
from omnimarket.nodes.node_captured_secret_store_effect.models.model_captured_secret_store_request import (
    ModelCapturedSecretStoreRequest,
)
from omnimarket.nodes.node_captured_secret_store_effect.models.model_captured_secret_store_result import (
    EnumCapturedSecretStoreOutcome,
    ModelCapturedSecretStoreResult,
)
from omnimarket.nodes.node_event_emit_effect.redaction import (
    SecretReference,
    load_contract,
)
from omnimarket.nodes.node_secret_store_effect import HandlerSecretStore
from omnimarket.nodes.node_secret_store_effect.models import (
    EnumSecretStoreOperation,
    EnumSecretStoreOutcome,
    ModelSecretStoreOverlay,
    ModelSecretStoreRequest,
)

logger = logging.getLogger(__name__)

#: One API call may take this long. A store that does not answer must not hold
#: a backgrounded capture process open.
TIMEOUT_SECONDS = 5.0


class CapturedSecretWriterConfigError(ValueError):
    """The writer configuration is present and unusable. Names keys, never values."""


@dataclass(frozen=True)
class CapturedSecretWriterConfig:
    """The writer's resolved configuration. Secrets are excluded from repr."""

    provider: str
    infisical_addr: str
    project_id: str
    environment_slug: str
    secret_path: str
    client_id: str
    client_secret: SecretStr = field(repr=False)
    reference_key: SecretStr = field(repr=False)


def load_writer_config(onex_home: Path) -> CapturedSecretWriterConfig | None:
    """The writer configuration, or ``None`` when this machine has none.

    ``None`` means no ``captured_secret_store`` block, or a block naming no
    writer identity: this machine does not take part.

    Raises:
        CapturedSecretWriterConfigError: the block is present but unusable.
    """
    from omnibase_core.errors.model_onex_error import ModelOnexError
    from omnibase_infra.cli.store_onex_home_files import StoreOnexHomeFiles

    files = StoreOnexHomeFiles(onex_home)
    try:
        document = files.load_config(must_exist=False)
    except ModelOnexError as exc:
        raise CapturedSecretWriterConfigError(exc.message) from None
    if CAPTURED_SECRET_STORE_BLOCK not in document:
        return None
    try:
        overlay = load_captured_secret_store_overlay(
            document, source=str(files.config_path)
        )
    except ValueError as exc:
        raise CapturedSecretWriterConfigError(str(exc)) from None
    if overlay.writer_client_id is None:
        return None
    if overlay.provider is None:
        raise CapturedSecretWriterConfigError(
            f"{files.config_path} block '{CAPTURED_SECRET_STORE_BLOCK}' names a "
            "writer but no provider"
        )
    named = {
        "writer_client_secret_ref": overlay.writer_client_secret_ref,
        "reference_key_ref": overlay.reference_key_ref,
    }
    refs = {name: ref for name, ref in named.items() if ref is not None}
    missing = sorted(set(named) - set(refs))
    if missing:
        raise CapturedSecretWriterConfigError(
            f"{files.config_path} block '{CAPTURED_SECRET_STORE_BLOCK}' names a "
            f"writer but not {missing}"
        )
    values: dict[str, SecretStr] = {}
    for name, ref in refs.items():
        remediation = f"file it in credentials.json under '{ref}' (mode 0600)"
        try:
            values[name] = SecretStr(files.read_secret(ref, remediation))
        except ModelOnexError as exc:
            raise CapturedSecretWriterConfigError(exc.message) from None
    return CapturedSecretWriterConfig(
        provider=overlay.provider,
        infisical_addr=overlay.infisical_addr.rstrip("/"),
        project_id=str(overlay.project_id),
        environment_slug=overlay.environment_slug,
        secret_path=overlay.secret_path,
        client_id=overlay.writer_client_id,
        client_secret=values["writer_client_secret_ref"],
        reference_key=values["reference_key_ref"],
    )


# ---------------------------------------------------------------------------
# the store, through node_secret_store_effect
# ---------------------------------------------------------------------------

#: The names the writer's two loaded credentials go by in the node's overlay.
_CLIENT_ID_REF = "captured-writer-client-id"
_CLIENT_SECRET_REF = "captured-writer-client-secret"


class _HeldWriterCredentials:
    """The writer identity, already loaded, as the node's bootstrap store.

    Read only, and it answers only the two names the overlay gives the node.
    """

    def __init__(self, config: CapturedSecretWriterConfig) -> None:
        self._values = {
            _CLIENT_ID_REF: SecretStr(config.client_id),
            _CLIENT_SECRET_REF: config.client_secret,
        }

    async def get_secret(self, key: str) -> str | None:
        held = self._values.get(key)
        return None if held is None else held.get_secret_value()

    async def set_secret(self, key: str, value: str) -> bool:
        raise RuntimeError("the writer's credential holder is read only")

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("the writer's credential holder is read only")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        return []

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        return None


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run the node's async handler from this sync one, inside a loop or not."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


def _secret_store_node(
    config: CapturedSecretWriterConfig, store: ProtocolSecretStore | None
) -> HandlerSecretStore:
    overlay = ModelSecretStoreOverlay(
        provider=config.provider,
        address=config.infisical_addr,
        project_id=config.project_id,
        environment=config.environment_slug,
        client_id_ref=_CLIENT_ID_REF,
        client_secret_ref=_CLIENT_SECRET_REF,
        timeout_seconds=TIMEOUT_SECONDS,
    )
    return HandlerSecretStore(
        store=store,
        overlay=overlay,
        bootstrap_store=_HeldWriterCredentials(config),
    )


_NODE_REFUSALS: dict[EnumSecretStoreOutcome, EnumCapturedSecretStoreOutcome] = {
    EnumSecretStoreOutcome.UNREACHABLE: EnumCapturedSecretStoreOutcome.STORE_UNREACHABLE,
    EnumSecretStoreOutcome.NOT_CONFIGURED: EnumCapturedSecretStoreOutcome.MISCONFIGURED,
    EnumSecretStoreOutcome.MISCONFIGURED: EnumCapturedSecretStoreOutcome.MISCONFIGURED,
    EnumSecretStoreOutcome.INVALID_REQUEST: EnumCapturedSecretStoreOutcome.MISCONFIGURED,
}


# ---------------------------------------------------------------------------
# the handler
# ---------------------------------------------------------------------------


class HandlerCapturedSecretStore:
    """``handle(request) -> result``: one secret in, one reference or a reason out.

    Loads its configuration on the first request, so a producer can build one
    per capture and pay nothing when no secret is found. Logs in once, caches
    the references it minted, and after the first failure refuses for the rest
    of its life.
    """

    def __init__(
        self,
        *,
        onex_home: Path,
        store: ProtocolSecretStore | None = None,
        contract_path: Path | None = None,
    ) -> None:
        self._onex_home = onex_home
        self._store = store
        self._contract_path = contract_path
        self._loaded = False
        self._config: CapturedSecretWriterConfig | None = None
        self._reference: SecretReference | None = None
        self._node: HandlerSecretStore | None = None
        self._minted: dict[str, str] = {}
        self._refusal: ModelCapturedSecretStoreResult | None = None

    def _refuse(
        self, outcome: EnumCapturedSecretStoreOutcome, detail: str | None = None
    ) -> ModelCapturedSecretStoreResult:
        result = ModelCapturedSecretStoreResult(outcome=outcome, detail=detail)
        self._refusal = result
        logger.warning("captured secret not stored: %s (%s)", outcome.value, detail)
        return result

    def _configure(self) -> ModelCapturedSecretStoreResult | None:
        """Load config and contract once; a refusal if this machine cannot write."""
        if self._loaded:
            return None
        self._loaded = True
        self._reference = load_contract(self._contract_path).secret_reference
        if self._reference is None:
            return self._refuse(
                EnumCapturedSecretStoreOutcome.MISCONFIGURED,
                "the capture-redaction contract declares no secret_reference",
            )
        try:
            self._config = load_writer_config(self._onex_home)
        except CapturedSecretWriterConfigError as exc:
            return self._refuse(EnumCapturedSecretStoreOutcome.MISCONFIGURED, str(exc))
        if self._config is None:
            return self._refuse(EnumCapturedSecretStoreOutcome.NOT_CONFIGURED)
        self._node = _secret_store_node(self._config, self._store)
        return None

    def handle(
        self, request: ModelCapturedSecretStoreRequest
    ) -> ModelCapturedSecretStoreResult:
        if self._refusal is not None:
            return self._refusal
        refused = self._configure()
        if refused is not None:
            return refused
        config, reference = self._config, self._reference
        if config is None or reference is None:  # unreachable after _configure
            return self._refuse(EnumCapturedSecretStoreOutcome.MISCONFIGURED)
        digest = hmac.new(
            config.reference_key.get_secret_value().encode("utf-8"),
            request.value.get_secret_value().encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        minted = self._minted.get(digest)
        if minted is not None:
            return ModelCapturedSecretStoreResult(
                outcome=EnumCapturedSecretStoreOutcome.EXISTS, reference=minted
            )
        node = self._node
        if node is None:  # unreachable after _configure
            return self._refuse(EnumCapturedSecretStoreOutcome.MISCONFIGURED)
        stored = _run(
            node.handle(
                ModelSecretStoreRequest(
                    operation=EnumSecretStoreOperation.CREATE,
                    folder=config.secret_path,
                    key=reference.store_key_for(digest),
                    value=request.value,
                )
            )
        )
        if stored.outcome is EnumSecretStoreOutcome.REFUSED:
            login = "login" in (stored.detail or "")
            return self._refuse(
                EnumCapturedSecretStoreOutcome.LOGIN_REFUSED
                if login
                else EnumCapturedSecretStoreOutcome.WRITE_REFUSED
            )
        if stored.outcome not in (
            EnumSecretStoreOutcome.CREATED,
            EnumSecretStoreOutcome.EXISTS,
        ):
            return self._refuse(
                _NODE_REFUSALS.get(
                    stored.outcome, EnumCapturedSecretStoreOutcome.WRITE_FAILED
                ),
                stored.detail,
            )
        minted = reference.reference_for(digest)
        self._minted[digest] = minted
        return ModelCapturedSecretStoreResult(
            outcome=(
                EnumCapturedSecretStoreOutcome.STORED
                if stored.outcome is EnumSecretStoreOutcome.CREATED
                else EnumCapturedSecretStoreOutcome.EXISTS
            ),
            reference=minted,
        )


__all__ = [
    "CapturedSecretWriterConfig",
    "CapturedSecretWriterConfigError",
    "HandlerCapturedSecretStore",
    "load_writer_config",
]
