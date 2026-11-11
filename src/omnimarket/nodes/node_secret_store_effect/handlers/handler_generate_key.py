# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Generated keys: born inside the runtime, stored create-only (OMN-23695).

``handle(request: ModelGenerateKeyRequest) -> ModelGenerateKeyResult``. A dev
key that can be generated -- the probe tenant key scenario R8 reads -- is
generated IN the runtime and stored through :class:`HandlerSecretStore`, the
node's one store seam, create-only: an existing key is reported, never replaced
and never read. Nobody sets the value, holds it or types it: it is
``prefix + token_source(bytes)`` of the contract's ``generated_keys`` entry, and
every part of that entry (folder, key, bytes, prefix) is the contract's, so none
is written in code.

The value is never printed, serialized, logged or put on a command line
------------------------------------------------------------------------
The result model has no field that can hold it. The value lives in a
``SecretStr`` for the length of one ``create`` request and is then only in the
store. Logs and ``detail`` name the id, the folder, the key NAME and the
outcome, never the value; a store exception is reported by its type alone (the
store handler's own posture), because a foreign store may echo a request.

An id the contract does not declare is ``invalid_request`` naming the id;
nothing is generated and nothing is stored. Every other store outcome passes
through with its own detail.

The machine identity is read exactly as ``handler_house_model_key`` reads it:
the machine's own ``secret_store`` block first, then, with no block, the store
addressing and identity the host's own ``~/.omnibase/.env`` already carries.
With an overlay document the store block is that document's and the host env
file fallback never runs.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import SecretStr

from omnimarket.config.service_endpoints import service_endpoint_url
from omnimarket.models.secret_store.model_generate_key_request import (
    ModelGenerateKeyRequest,
)
from omnimarket.models.secret_store.model_generate_key_result import (
    ModelGenerateKeyResult,
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
)
from omnimarket.nodes.node_secret_store_effect.adapters import IdentityFileStore
from omnimarket.nodes.node_secret_store_effect.handlers.handler_house_model_key import (
    HOST_ENV_FILE,
    host_env_overlay,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_REFERENCE_PREFIX = service_endpoint_url("internal_targets.secret_reference_prefix")


@dataclass(frozen=True)
class _DeclaredKey:
    """One ``generated_keys`` entry: every value comes from the contract."""

    folder: str
    key: str
    bytes: int
    prefix: str


def _load_generated_keys(contract_path: Path) -> dict[str, _DeclaredKey]:
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    block = raw.get("generated_keys") if isinstance(raw, dict) else None
    declared: dict[str, _DeclaredKey] = {}
    for name, spec in (block or {}).items():
        if not isinstance(spec, dict):
            raise ValueError(f"{contract_path}: generated key {name!r} malformed")
        declared[str(name)] = _DeclaredKey(
            folder=str(spec["folder"]),
            key=str(spec["key"]),
            bytes=int(spec["bytes"]),
            prefix=str(spec.get("prefix", "")),
        )
    return declared


def generated_key_reference(folder: str, key: str) -> str:
    """The ``secret://onex`` reference a generated key is stored as."""
    return f"{_REFERENCE_PREFIX}{folder.lstrip('/')}/{key}"


class HandlerGenerateKey:
    """EFFECT: generate one declared key and store it create-only, value unseen.

    Args:
        store_handler: The secret store handler to ask. With none, the overlay
            document's store block when one is given, else the machine's own
            overlay first and the host env file second.
        overlay: The store block to store through; with it, the host env file
            fallback never runs.
        onex_home: The ONEX home holding ``config.yaml``; defaults to the standard one.
        home: The user's home holding ``.omnibase/.env``; defaults to ``Path.home()``.
        contract_path: The node contract, for ``generated_keys``.
        token_source: The random token source; injectable so a test can hold the
            value it returned. Its output is never returned, only stored.
    """

    def __init__(
        self,
        *,
        store_handler: HandlerSecretStore | None = None,
        overlay: ModelSecretStoreOverlay | None = None,
        onex_home: Path | None = None,
        home: Path | None = None,
        contract_path: Path | None = None,
        token_source: Callable[[int], str] = secrets.token_urlsafe,
    ) -> None:
        self._store_handler = store_handler
        self._overlay = overlay
        self._onex_home = onex_home
        self._home = home
        self._declared = _load_generated_keys(contract_path or _CONTRACT_PATH)
        self._token_source = token_source

    async def handle(self, request: ModelGenerateKeyRequest) -> ModelGenerateKeyResult:
        def result(
            outcome: EnumSecretStoreOutcome,
            *,
            folder: str | None = None,
            key: str | None = None,
            key_length: int | None = None,
            detail: str | None = None,
        ) -> ModelGenerateKeyResult:
            logger.info(
                "generate key id=%s folder=%s key=%s outcome=%s%s",
                request.name,
                folder,
                key,
                outcome.value,
                f" detail={detail}" if detail else "",
            )
            return ModelGenerateKeyResult(
                name=request.name,
                reference=generated_key_reference(folder, key)
                if folder and key
                else f"{_REFERENCE_PREFIX}{request.name}",
                outcome=outcome,
                key_length=key_length,
                folder=folder,
                key=key,
                detail=detail,
                correlation_id=request.correlation_id,
            )

        declared = self._declared.get(request.name)
        if declared is None:
            return result(
                EnumSecretStoreOutcome.INVALID_REQUEST,
                detail=(
                    f"the contract declares no generated key {request.name!r}; "
                    "nothing was generated and nothing was stored"
                ),
            )
        # The value exists only between these two lines and inside the store.
        value = SecretStr(declared.prefix + self._token_source(declared.bytes))
        store_request = ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.CREATE,
            folder=declared.folder,
            key=declared.key,
            value=value,
            correlation_id=request.correlation_id,
        )
        handler = self._store_handler or HandlerSecretStore(
            overlay=self._overlay, onex_home=self._onex_home
        )
        store_result = await handler.handle(store_request)
        if (
            store_result.outcome is EnumSecretStoreOutcome.NOT_CONFIGURED
            and self._store_handler is None
            and self._overlay is None
        ):
            home = self._home or Path.home()
            overlay = host_env_overlay(home)
            if overlay is not None:
                store_result = await HandlerSecretStore(
                    overlay=overlay,
                    onex_home=self._onex_home,
                    bootstrap_store=IdentityFileStore(home / HOST_ENV_FILE),
                ).handle(store_request)
        folder = store_result.folder or declared.folder
        key = store_result.key or declared.key
        if store_result.outcome is EnumSecretStoreOutcome.CREATED:
            return result(
                store_result.outcome,
                folder=folder,
                key=key,
                key_length=len(value.get_secret_value()),
                detail=store_result.detail,
            )
        if store_result.outcome is EnumSecretStoreOutcome.EXISTS:
            return result(
                store_result.outcome,
                folder=folder,
                key=key,
                detail=(
                    store_result.detail
                    or "the key already exists; nothing was replaced and nothing was read"
                ),
            )
        return result(
            store_result.outcome,
            folder=folder,
            key=key,
            detail=store_result.detail,
        )


__all__ = ["HandlerGenerateKey", "generated_key_reference"]
