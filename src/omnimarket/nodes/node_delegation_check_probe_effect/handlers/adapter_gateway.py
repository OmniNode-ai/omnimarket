# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live gateway client of the delegation check probe node (OMN-26642).

The address is the settings store's ``delegation_check_gateway`` row and the credential is the test tenant's token, found by the
dev tenant node's overlay (the tenant's slug, the folder of its tokens and the secret name of its token). Nothing here names a
host: a client is built from the row and the credential source it is given.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any, Final, Protocol

from pydantic import SecretStr

from omnimarket.handlers.delegation_scenario_settings import SecretStoreSettingsReader
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.overlay_documents.model_delegation_check_gateway_overlay import (
    ModelDelegationCheckGatewayOverlay,
    ModelDevTenantTokenSource,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_gateway_check import (
    ModelGatewayObservation,
)
from omnimarket.nodes.node_delegation_check_probe_effect.protocols.protocol_delegation_check import (
    CollectorAbsentError,
)

GATEWAY_OVERLAY_ID: Final = "delegation_check_gateway"
GATEWAY_FILE_ENV: Final = "ONEX_DELEGATION_CHECK_GATEWAY_OVERLAY"
DEV_TENANT_NODE: Final = "node_dev_proof_tenant_token_effect"
API_KEY_HEADER: Final = "x-api-key"
AUTHORIZATION_HEADER: Final = "authorization"
#: A credential no tenant owns, in the gateway's key shape; it never authenticates.
INVALID_CREDENTIAL: Final = "onxk_" + "0" * 32
BODY_LIMIT: Final = 1 << 20


class ProtocolSecretReader(Protocol):
    async def read(self, folder: str, name: str) -> SecretStr | None: ...


class ProtocolTenantCredential(Protocol):
    def token(self, tenant_slug: str) -> str: ...


class DevTenantCredential:
    """The test tenant's token, by the secret name the dev tenant node's overlay declares for its slug."""

    def __init__(
        self,
        source: ModelDevTenantTokenSource | None = None,
        reader: ProtocolSecretReader | None = None,
        secret_name: str | None = None,
    ) -> None:
        self._source = source
        self._reader = reader
        # The gateway row's secret name for the credential; with none the dev tenant node's overlay names it.
        self._secret_name = secret_name
        self._cached: dict[str, str] = {}

    def _declared(self) -> ModelDevTenantTokenSource:
        if self._source is None:
            self._source = load_structured_overlay(
                DEV_TENANT_NODE, ModelDevTenantTokenSource
            )
        return self._source

    def token(self, tenant_slug: str) -> str:
        if tenant_slug in self._cached:
            return self._cached[tenant_slug]
        try:
            declared = self._declared()
        except ValueError as exc:
            raise CollectorAbsentError(
                "tenant_credential",
                f"the dev tenant node's overlay cannot be read: {type(exc).__name__}",
            ) from None
        secret_name = self._secret_name
        if secret_name is None:
            row = next(
                (t for t in declared.tenants if t.tenant_slug == tenant_slug), None
            )
            if row is None:
                raise CollectorAbsentError(
                    "tenant_credential",
                    "the dev tenant node's overlay does not declare the tenant the gateway row names",
                )
            secret_name = row.secret_name
        reader = self._reader or SecretStoreSettingsReader(overlay=declared.store)
        try:
            secret = asyncio.run(reader.read(declared.token_folder, secret_name))
        except Exception as exc:
            raise CollectorAbsentError(
                "tenant_credential",
                f"the dev secret store cannot be read: {type(exc).__name__}",
            ) from None
        if secret is None:
            raise CollectorAbsentError(
                "tenant_credential",
                "the tenant's token is not in the dev secret store under its declared name",
            )
        self._cached[tenant_slug] = secret.get_secret_value()
        return self._cached[tenant_slug]


class HttpGatewayCaller:
    """One request at a time to the gateway the row names; an unanswered request is an observation with an error."""

    def __init__(
        self,
        row: ModelDelegationCheckGatewayOverlay,
        credential: ProtocolTenantCredential,
        timeout_s: float,
        submit_timeout_s: float | None = None,
    ) -> None:
        self.gateway_url = row.gateway_url
        self.tenant_slug = row.tenant_slug
        self.chosen_provider: str | None = row.chosen_provider
        self._credential_kind = row.credential_kind
        self._credential = credential
        self._timeout_s = timeout_s
        # The workflow submit has its own cap (overlay ``submit_timeout_s``); absent, it shares the responder cap.
        self._submit_timeout_s = (
            timeout_s if submit_timeout_s is None else submit_timeout_s
        )

    def _credential_headers(self, value: str) -> dict[str, str]:
        """An API key goes in the API key header; a signed JWT goes in the Authorization header as a bearer token."""
        if self._credential_kind == "jwt":
            return {AUTHORIZATION_HEADER: f"Bearer {value}"}
        return {API_KEY_HEADER: value}

    def call(
        self,
        method: str,
        path: str,
        *,
        auth: str,
        body: dict[str, Any] | None = None,
    ) -> ModelGatewayObservation:
        headers: dict[str, str] = {"accept": "application/json"}
        if auth == "tenant":
            headers.update(
                self._credential_headers(self._credential.token(self.tenant_slug))
            )
        elif auth == "invalid":
            headers.update(self._credential_headers(INVALID_CREDENTIAL))
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["content-type"] = "application/json"
        request = urllib.request.Request(
            self.gateway_url + path, data=data, headers=headers, method=method
        )
        timeout_s = (
            self._submit_timeout_s
            if method == "POST" and path == "/v1/workflows"
            else self._timeout_s
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return ModelGatewayObservation(
                    status=response.status, body=_parse(response.read(BODY_LIMIT))
                )
        except urllib.error.HTTPError as exc:
            return ModelGatewayObservation(
                status=exc.code, body=_parse(exc.read(BODY_LIMIT))
            )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # The exception type, never its message: it can echo the request, and a request carries a credential.
            return ModelGatewayObservation(status=None, error=type(exc).__name__)


def _parse(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None


def load_gateway_row(
    environ: Mapping[str, str] | None = None,
    *,
    explicit: object | None = None,
) -> ModelDelegationCheckGatewayOverlay:
    """The settings store's gateway row; with none served the node loads no gateway and its checks read PRECONDITION-ABSENT."""
    return load_structured_overlay(
        GATEWAY_OVERLAY_ID,
        ModelDelegationCheckGatewayOverlay,
        environ,
        explicit=explicit,
        file_env=GATEWAY_FILE_ENV,
    )


__all__ = [
    "GATEWAY_OVERLAY_ID",
    "DevTenantCredential",
    "HttpGatewayCaller",
    "ProtocolSecretReader",
    "ProtocolTenantCredential",
    "load_gateway_row",
]
