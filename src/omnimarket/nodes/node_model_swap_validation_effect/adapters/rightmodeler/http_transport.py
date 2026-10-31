# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One bounded, authenticated POST to an explicitly configured service."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import httpx
from omnibase_core.enums.enum_model_swap_validation_failure import (
    EnumModelSwapValidationFailure,
)

from omnimarket.inference.secret_store_resolver import (
    resolve_api_key_async,
    secret_store_from_config_path,
)
from omnimarket.nodes.node_model_swap_validation_effect.models.model_model_swap_service_config import (
    ModelModelSwapServiceConfig,
)

if TYPE_CHECKING:
    from omnibase_spi.protocols.services import ProtocolSecretStore


class RightModelerServiceTransportError(Exception):
    """A sanitized transport refusal, containing no response or exception text."""

    def __init__(self, failure: EnumModelSwapValidationFailure, code: str) -> None:
        super().__init__(code)
        self.failure = failure
        self.code = code


def _contains_secret(value: object, secret: str) -> bool:
    if isinstance(value, str):
        return secret in value
    if isinstance(value, dict):
        return any(
            _contains_secret(key, secret) or _contains_secret(item, secret)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret(item, secret) for item in value)
    return False


class RightModelerHttpTransport:
    """Resolve one logical credential and send one request without retries."""

    def __init__(
        self,
        config: ModelModelSwapServiceConfig,
        *,
        secret_store: ProtocolSecretStore | None = None,
    ) -> None:
        self.config = config
        self.secret_store = secret_store

    async def post(self, payload: bytes) -> bytes:
        """Return bounded response bytes, without following redirects."""
        if len(payload) > self.config.max_request_bytes:
            raise RightModelerServiceTransportError(
                EnumModelSwapValidationFailure.NEEDS_INPUT, "service_request_too_large"
            )
        try:
            async with asyncio.timeout(self.config.deadline_seconds):
                return await self._post(payload)
        except (TimeoutError, httpx.TimeoutException):
            raise RightModelerServiceTransportError(
                EnumModelSwapValidationFailure.TIMEOUT, "service_timeout"
            ) from None
        except httpx.HTTPError:
            raise RightModelerServiceTransportError(
                EnumModelSwapValidationFailure.TOOL_FAILED, "service_transport_failed"
            ) from None

    async def _post(self, payload: bytes) -> bytes:
        try:
            store = self.secret_store
            if store is None:
                path = self.config.secret_resolver_config_path
                if path is None:
                    raise ValueError("An explicit secret store is required")
                store = secret_store_from_config_path(path)
            credential = await resolve_api_key_async(
                self.config.secret_ref,
                store=store,
                required=True,
                env_var_fallback=None,
            )
            if credential is None or not credential.get_secret_value():
                raise ValueError("Service credential is unavailable")
            secret = credential.get_secret_value()
        except Exception:
            raise RightModelerServiceTransportError(
                EnumModelSwapValidationFailure.NEEDS_INPUT,
                "service_credentials_unavailable",
            ) from None

        timeout = httpx.Timeout(
            connect=self.config.connect_timeout_seconds,
            read=self.config.read_timeout_seconds,
            write=self.config.write_timeout_seconds,
            pool=self.config.pool_timeout_seconds,
        )
        async with (
            httpx.AsyncClient(
                timeout=timeout, follow_redirects=False, trust_env=False
            ) as client,
            client.stream(
                "POST",
                self.config.endpoint_ref,
                content=payload,
                headers={
                    "authorization": f"Bearer {secret}",
                    "content-type": "application/json",
                    "accept": "application/json",
                    "accept-encoding": "identity",
                },
            ) as response,
        ):
            if response.status_code != 200:
                raise RightModelerServiceTransportError(
                    EnumModelSwapValidationFailure.TOOL_FAILED,
                    "service_http_error",
                )
            encoding = response.headers.get("content-encoding", "").strip().lower()
            if encoding not in {"", "identity"}:
                raise RightModelerServiceTransportError(
                    EnumModelSwapValidationFailure.SCHEMA_MISMATCH,
                    "service_unsupported_encoding",
                )
            body = bytearray()
            async for chunk in response.aiter_raw(
                chunk_size=self.config.response_chunk_bytes
            ):
                if len(body) + len(chunk) > self.config.max_response_bytes:
                    raise RightModelerServiceTransportError(
                        EnumModelSwapValidationFailure.SCHEMA_MISMATCH,
                        "service_response_too_large",
                    )
                body.extend(chunk)
        try:
            decoded: object = json.loads(body)
            if secret.encode() in body or _contains_secret(decoded, secret):
                raise RightModelerServiceTransportError(
                    EnumModelSwapValidationFailure.SCHEMA_MISMATCH,
                    "service_credential_echo",
                )
        except (ValueError, UnicodeError, RecursionError):
            raise RightModelerServiceTransportError(
                EnumModelSwapValidationFailure.SCHEMA_MISMATCH,
                "service_invalid_json",
            ) from None
        return bytes(body)


__all__ = ["RightModelerHttpTransport", "RightModelerServiceTransportError"]
