# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""House model keys come from the secret store, on every host (OMN-22417).

``handle(request: ModelHouseModelKeyRequest) -> ModelHouseModelKeyResult``. A
house inference-provider key (``LLM_GLM_API_KEY``) is the secret of that name in
the store's house folder; this handler resolves it through
:class:`HandlerSecretStore`, the node's one store seam, so a host with no local
copy of the key still reads it.

Which identity asks the store
-----------------------------
1. The machine's own ``secret_store`` block in ``<onex_home>/config.yaml``.
2. With no block, the store addressing and the identity NAMES the host's own
   ``~/.omnibase/.env`` already carries (``INFISICAL_ADDR``,
   ``INFISICAL_PROJECT_ID``, ``INFISICAL_ENVIRONMENT_SLUG``,
   ``INFISICAL_CLIENT_ID``, ``INFISICAL_CLIENT_SECRET``). That file is read in
   process by the identity-file store, refused unless mode 0600.

This is the same identity read two ways, never a different key. There is no
fallback to a local copy of the model key, to another folder or to another key:
a key the store does not hold is ``not_found`` naming the reference, and a store
that cannot be asked keeps its own typed outcome.

The value moves in process only.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr

from omnimarket.config.service_endpoints import service_endpoint_url
from omnimarket.models.secret_store.model_house_model_key_request import (
    ModelHouseModelKeyRequest,
)
from omnimarket.models.secret_store.model_house_model_key_result import (
    EnumHouseKeyIdentitySource,
    ModelHouseModelKeyResult,
)
from omnimarket.models.secret_store.model_secret_resolution_error import (
    SecretStoreRefusedError,
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
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)

#: The folder of the house's own environment keys, relative to the overlay root.
HOUSE_KEY_FOLDER = "/shared/env"

#: The host file that already carries the store identity, relative to the home.
HOST_ENV_FILE = ".omnibase/.env"

_REFERENCE_PREFIX = service_endpoint_url("internal_targets.secret_reference_prefix")


def house_key_reference(name: str) -> str:
    """The ``secret://onex`` reference of a house key, for refusals and receipts."""
    return f"{_REFERENCE_PREFIX}{HOUSE_KEY_FOLDER.lstrip('/')}/{name}"


def _env_file_values(path: Path, wanted: frozenset[str]) -> dict[str, str]:
    """The non-secret addressing values of ``path`` (never the identity itself)."""
    found: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return found
    for line in lines:
        name, sep, raw = line.strip().removeprefix("export ").partition("=")
        name = name.strip()
        if sep and name in wanted:
            found[name] = raw.strip().strip("\"'")
    return found


def host_env_overlay(home: Path) -> ModelSecretStoreOverlay | None:
    """The overlay the host's own env file implies, or ``None`` when it names no store."""
    addressing = _env_file_values(
        home / HOST_ENV_FILE,
        frozenset(
            {"INFISICAL_ADDR", "INFISICAL_PROJECT_ID", "INFISICAL_ENVIRONMENT_SLUG"}
        ),
    )
    if len(addressing) < 3:
        return None
    try:
        return ModelSecretStoreOverlay(
            provider="infisical",
            address=addressing.get("INFISICAL_ADDR"),
            project_id=addressing.get("INFISICAL_PROJECT_ID"),
            environment=addressing.get("INFISICAL_ENVIRONMENT_SLUG"),
            client_id_ref="INFISICAL_CLIENT_ID",
            client_secret_ref="INFISICAL_CLIENT_SECRET",
            identity_file=HOST_ENV_FILE,
        )
    except ValueError:
        return None


class HandlerHouseModelKey:
    """EFFECT: resolve one house model key from the house folder of the store.

    Args:
        store_handler: The secret store handler to ask. With none, the machine's
            own overlay is tried first and the host env file second.
        onex_home: The ONEX home holding ``config.yaml``; defaults to the standard one.
        home: The user's home holding ``.omnibase/.env``; defaults to ``Path.home()``.
    """

    def __init__(
        self,
        *,
        store_handler: HandlerSecretStore | None = None,
        onex_home: Path | None = None,
        home: Path | None = None,
    ) -> None:
        self._store_handler = store_handler
        self._onex_home = onex_home
        self._home = home

    async def handle(
        self, request: ModelHouseModelKeyRequest
    ) -> ModelHouseModelKeyResult:
        reference = house_key_reference(request.name)
        store_request = ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.RESOLVE,
            folder=HOUSE_KEY_FOLDER,
            key=request.name,
            correlation_id=request.correlation_id,
        )
        source = EnumHouseKeyIdentitySource.OVERLAY
        handler = self._store_handler or HandlerSecretStore(onex_home=self._onex_home)
        result = await handler.handle(store_request)
        if (
            result.outcome is EnumSecretStoreOutcome.NOT_CONFIGURED
            and self._store_handler is None
        ):
            overlay = host_env_overlay(self._home or Path.home())
            if overlay is None:
                return ModelHouseModelKeyResult(
                    name=request.name,
                    reference=reference,
                    outcome=EnumSecretStoreOutcome.NOT_CONFIGURED,
                    identity_source=EnumHouseKeyIdentitySource.NONE,
                    detail=(
                        f"no secret_store block in config.yaml and no store addressing "
                        f"in ~/{HOST_ENV_FILE}; {reference} cannot be read"
                    ),
                    correlation_id=request.correlation_id,
                )
            source = EnumHouseKeyIdentitySource.HOST_ENV_FILE
            result = await HandlerSecretStore(
                overlay=overlay, onex_home=self._onex_home
            ).handle(store_request)
        value = result.value
        if result.outcome is EnumSecretStoreOutcome.RESOLVED and value is None:
            return ModelHouseModelKeyResult(
                name=request.name,
                reference=reference,
                outcome=EnumSecretStoreOutcome.FAILED,
                identity_source=source,
                detail="the store answered resolved with no value",
                correlation_id=request.correlation_id,
            )
        return ModelHouseModelKeyResult(
            name=request.name,
            reference=reference,
            outcome=result.outcome,
            identity_source=source,
            value=value,
            value_length=len(value.get_secret_value()) if value is not None else None,
            detail=result.detail,
            correlation_id=request.correlation_id,
        )


async def require_house_model_key(
    name: str, *, handler: HandlerHouseModelKey | None = None
) -> SecretStr:
    """The key's value, or a typed refusal that names the secret reference.

    Raises:
        SecretStoreRefusedError: the store holds no such key, or could not be asked.
    """
    result = await (handler or HandlerHouseModelKey()).handle(
        ModelHouseModelKeyRequest(name=name)
    )
    if result.value is None or not result.ok:
        detail = f" ({result.detail})" if result.detail else ""
        raise SecretStoreRefusedError(
            f"House model key {result.reference} could not be read from the secret "
            f"store: it answered {result.outcome.value}{detail}. No fallback is permitted.",
            outcome=result.outcome.value,
        )
    return result.value


__all__ = [
    "HOST_ENV_FILE",
    "HOUSE_KEY_FOLDER",
    "HandlerHouseModelKey",
    "host_env_overlay",
    "house_key_reference",
    "require_house_model_key",
]
