# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev secret store's ``gateway_key`` folder, written create-only through the secret store node's own handler.

A create's value never travels on the bus (the secret store node's request excludes it from serialization), so the write
is the in-process call that node documents for an operator's command line. The handler reads the store address and the
machine identity from this machine's ``secret_store`` overlay block; no store address or credential is named here.
"""

from __future__ import annotations

import asyncio

from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_infra.cli.store_onex_home_files import StoreOnexHomeFiles
from omnibase_infra.doctor.delegation_doctor_support import default_onex_home
from pydantic import SecretStr

from omnimarket.config.service_endpoints import service_endpoint_url
from omnimarket.models.secret_store.model_secret_store_overlay import (
    load_secret_store_overlay,
)
from omnimarket.models.secret_store.model_secret_store_request import (
    EnumSecretStoreOperation,
    ModelSecretStoreRequest,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)

_NAMESPACE = "gateway_key"


class SecretStoreGatewayKeys:
    """``ProtocolDevSecretStore`` over ``HandlerSecretStore``."""

    def __init__(self, handler: HandlerSecretStore | None = None) -> None:
        self._handler = handler or HandlerSecretStore()

    def environment(self) -> str | None:
        files = StoreOnexHomeFiles(default_onex_home())
        try:
            overlay = load_secret_store_overlay(
                files.load_config(must_exist=False), source=str(files.config_path)
            )
        except (ModelOnexError, ValueError):
            return None
        return overlay.environment if overlay is not None else None

    def create(self, name: str, value: SecretStr) -> str:
        prefix = service_endpoint_url("internal_targets.secret_reference_prefix")
        result = asyncio.run(
            self._handler.handle(
                ModelSecretStoreRequest(
                    operation=EnumSecretStoreOperation.CREATE,
                    reference=f"{prefix}{_NAMESPACE}/{name}",
                    value=value,
                ),
                report_detail=False,
            )
        )
        return result.outcome.value


__all__: list[str] = ["SecretStoreGatewayKeys"]
