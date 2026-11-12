# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Secret-free requests, results and ownership for Claude's gateway binding."""

from __future__ import annotations

import contextlib
import ipaddress
import re
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ModelClaudeGatewaySettingsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    action: Literal["enable", "status", "disable"]
    gateway_url: str | None = None
    model: str | None = None
    credential_ref: str | None = None
    credential_helper: str | None = None
    environment: dict[str, str] = Field(default_factory=dict, repr=False, exclude=True)

    @field_validator("gateway_url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = urlsplit(value)
            port = parsed.port
            host = parsed.hostname
            local = host == "localhost"
            if host and not local:
                with contextlib.suppress(ValueError):
                    local = ipaddress.ip_address(host).is_loopback
            if (
                any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
                or parsed.scheme not in ("https", "http")
                or not host
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or "?" in value
                or "#" in value
                or (parsed.scheme == "http" and not local)
                or (port is not None and port < 1)
                or "\\" in value
            ):
                raise ValueError
        except ValueError:
            raise ValueError(
                "Gateway requires HTTPS or loopback HTTP without credentials, query or fragment"
            ) from None
        return value.rstrip("/")

    @field_validator("model", "credential_ref", "credential_helper")
    @classmethod
    def single_line(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise ValueError("Gateway choices must be non-empty single lines")
        return value

    @field_validator("credential_ref")
    @classmethod
    def public_reference(cls, value: str | None) -> str | None:
        if (
            value is not None
            and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}", value) is None
        ):
            raise ValueError("Invalid credential reference")
        return value

    @model_validator(mode="after")
    def complete_enable(self) -> Self:
        if self.action == "enable" and not all(
            (self.gateway_url, self.model, self.credential_ref, self.credential_helper)
        ):
            raise ValueError(
                "Enable requires gateway URL, model, credential reference and helper"
            )
        return self


class ModelClaudeGatewaySettingsResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["success", "failure", "action_required", "unavailable"]
    changed: bool = False
    enabled: bool = False
    gateway_url: str | None = None
    model: str | None = None
    conflicts: tuple[str, ...] = ()
    fix: str | None = None


class ModelClaudeGatewayOwnership(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    phase: Literal["enabling", "enabled", "disabled"]
    gateway_url: str
    model: str
    credential_ref: str
    credential_helper: str
    original_env_present: bool
    backup_name: str = Field(
        default="claude_gateway.settings.backup",
        pattern=r"^claude_gateway\.settings\.[a-zA-Z0-9.-]+$",
    )

    def enable_request(
        self, environment: dict[str, str]
    ) -> ModelClaudeGatewaySettingsRequest:
        return ModelClaudeGatewaySettingsRequest(
            action="enable",
            gateway_url=self.gateway_url,
            model=self.model,
            credential_ref=self.credential_ref,
            credential_helper=self.credential_helper,
            environment=environment,
        )

    @model_validator(mode="after")
    def validate_owned_values(self) -> Self:
        self.enable_request({})
        return self
