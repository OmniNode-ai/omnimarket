# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev signed-in read handler (OMN-24345): sign the dev probe user in, read one answer, report the status.

The password value moves only from the secret store to the Keycloak password grant; the access token moves only from
that grant to the read's Authorization header. Neither is printed, logged, serialized or returned. The result carries
the HTTP status and the answer's data state only, never a row. The dev-only guard refuses, before any secret read or
HTTP call, when the lane or realm is not the one the contract declares.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr

from omnimarket.handlers.delegation_scenario_settings import ProtocolSettingStore
from omnimarket.models.model_dev_identity_http import (
    ModelIdentityHttpRequest,
)
from omnimarket.nodes.node_dev_identity_provision_effect.protocols.protocol_dev_identity_transport import (
    ProtocolDevIdentityTransport,
)
from omnimarket.nodes.node_dev_signed_in_read_effect.models.model_dev_signed_in_read import (
    EnumDevSignedInReadOutcome,
    ModelDevSignedInReadCommand,
    ModelDevSignedInReadResult,
)
from omnimarket.nodes.node_dev_signed_in_read_effect.overlay import (
    ModelDevSignedInReadOverlay,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"

_UNAUTHORIZED_STATUSES = (401, 403)


def _load_dev_only(contract_path: Path | None) -> dict[str, Any]:
    """The contract's ``dev_only`` block; the guard reads it from the file, never from code."""
    path = contract_path or _CONTRACT_PATH
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    dev_only: dict[str, Any] = document["dev_only"]
    return dev_only


class HandlerDevSignedInRead:
    """Signs the dev probe user in with the password grant and reads the one declared route with its bearer."""

    def __init__(
        self,
        *,
        overlay: ModelDevSignedInReadOverlay,
        transport: ProtocolDevIdentityTransport,
        settings_store: ProtocolSettingStore,
        contract_path: Path | None = None,
    ) -> None:
        self._overlay = overlay
        self._transport = transport
        self._settings_store = settings_store
        self._contract_path = contract_path

    def _result(
        self,
        outcome: EnumDevSignedInReadOutcome,
        *,
        detail: str,
        user_name: str | None = None,
        http_status: int | None = None,
        data_state: str | None = None,
        expected_secret: str | None = None,
    ) -> ModelDevSignedInReadResult:
        ok = outcome in (
            EnumDevSignedInReadOutcome.READ_ACCEPTED,
            EnumDevSignedInReadOutcome.READY,
        )
        logger.info(
            "dev signed-in read outcome=%s realm=%s lane=%s user=%s status=%s",
            outcome.value,
            self._overlay.keycloak.realm,
            self._overlay.lane,
            user_name,
            http_status,
        )
        return ModelDevSignedInReadResult(
            ok=ok,
            outcome=outcome,
            detail=detail,
            realm=self._overlay.keycloak.realm,
            lane=self._overlay.lane,
            user_name=user_name,
            http_status=http_status,
            data_state=data_state,
            expected_secret=expected_secret,
        )

    def _dev_guard(
        self, command: ModelDevSignedInReadCommand
    ) -> ModelDevSignedInReadResult | None:
        dev_only = _load_dev_only(self._contract_path)
        declared_lane = dev_only["lane"]
        declared_realms = dev_only["realms"]
        if command.lane != declared_lane:
            return self._result(
                EnumDevSignedInReadOutcome.REFUSED_NOT_DEV,
                detail=f"command lane {command.lane!r} is not the declared dev lane {declared_lane!r}",
            )
        if self._overlay.lane != declared_lane:
            return self._result(
                EnumDevSignedInReadOutcome.REFUSED_NOT_DEV,
                detail=f"overlay lane {self._overlay.lane!r} is not the declared dev lane {declared_lane!r}",
            )
        if self._overlay.keycloak.realm not in declared_realms:
            return self._result(
                EnumDevSignedInReadOutcome.REFUSED_NOT_DEV,
                detail=f"realm {self._overlay.keycloak.realm!r} is not in the declared realms {declared_realms}",
            )
        return None

    async def handle(
        self, command: ModelDevSignedInReadCommand
    ) -> ModelDevSignedInReadResult:
        overlay = self._overlay
        kc = overlay.keycloak
        user = overlay.user
        read = overlay.read

        guard = self._dev_guard(command)
        if guard is not None:
            return guard

        password = await self._settings_store.read(
            user.password_folder, user.password_key
        )
        if password is None:
            return self._result(
                EnumDevSignedInReadOutcome.PASSWORD_MISSING,
                detail=f"the user password {user.password_folder}/{user.password_key} is not in the store",
                user_name=user.username,
                expected_secret=f"{user.password_folder}/{user.password_key}",
            )
        if not command.write:
            return self._result(
                EnumDevSignedInReadOutcome.READY,
                detail="the user password is present; the probe verb wrote nothing and called nothing",
                user_name=user.username,
            )

        token_url = f"{kc.base_url}{kc.token_path_template.format(realm=kc.realm)}"
        token = await self._user_token(token_url, password)
        if token is None:
            return self._result(
                EnumDevSignedInReadOutcome.USER_TOKEN_REFUSED,
                detail="the Keycloak password grant refused the dev user",
                user_name=user.username,
            )
        if isinstance(token, str):
            return self._result(
                EnumDevSignedInReadOutcome.KEYCLOAK_UNREACHABLE,
                detail=token,
                user_name=user.username,
            )

        response = await self._send(
            ModelIdentityHttpRequest(
                method="GET",
                url=f"{overlay.api.base_url}{read.path}",
                query={
                    read.correlation_param: read.correlation_id,
                    read.limit_param: str(read.limit),
                },
                bearer=token,
                timeout_seconds=overlay.api.timeout_seconds,
            )
        )
        if isinstance(response, str):
            return self._result(
                EnumDevSignedInReadOutcome.API_UNREACHABLE,
                detail=response,
                user_name=user.username,
            )
        if response.status in _UNAUTHORIZED_STATUSES:
            return self._result(
                EnumDevSignedInReadOutcome.READ_UNAUTHORIZED,
                detail=f"the read answered {response.status} to the signed-in bearer",
                user_name=user.username,
                http_status=response.status,
            )
        if response.status != 200:
            return self._result(
                EnumDevSignedInReadOutcome.READ_REFUSED,
                detail=f"the read answered {response.status}",
                user_name=user.username,
                http_status=response.status,
            )
        body = response.json_body
        state = body.get(read.data_state_field) if isinstance(body, dict) else None
        if not isinstance(state, str) or state not in read.data_states:
            return self._result(
                EnumDevSignedInReadOutcome.READ_RESPONSE_INVALID,
                detail=f"the read answered 200 without a {read.data_state_field} among {list(read.data_states)}",
                user_name=user.username,
                http_status=response.status,
            )
        return self._result(
            EnumDevSignedInReadOutcome.READ_ACCEPTED,
            detail="the signed-in read was accepted",
            user_name=user.username,
            http_status=response.status,
            data_state=state,
        )

    async def _send(self, request: ModelIdentityHttpRequest) -> Any:
        try:
            return await self._transport.send(request)
        except ConnectionError as exc:
            return f"no response arrived: {type(exc).__name__}"

    async def _user_token(self, token_url: str, password: SecretStr) -> Any:
        user = self._overlay.user
        response = await self._send(
            ModelIdentityHttpRequest(
                method="POST",
                url=token_url,
                form={
                    "grant_type": SecretStr("password"),
                    "client_id": SecretStr(user.client_id),
                    "username": SecretStr(user.username),
                    "password": password,
                },
                timeout_seconds=self._overlay.keycloak.timeout_seconds,
            )
        )
        if isinstance(response, str):
            return response
        if response.status != 200 or not isinstance(response.json_body, dict):
            return None
        access_token = response.json_body.get("access_token")
        if not isinstance(access_token, str):
            return None
        return SecretStr(access_token)


__all__ = ["HandlerDevSignedInRead"]
