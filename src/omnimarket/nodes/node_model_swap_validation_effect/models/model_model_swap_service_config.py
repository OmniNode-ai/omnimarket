# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Explicit deployment limits for an operator-provided validation service."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ModelModelSwapServiceConfig(BaseModel):
    """A full endpoint and logical credential, with no implicit network limits."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    endpoint_ref: str = Field(min_length=1)
    secret_ref: str = Field(pattern=r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")
    secret_resolver_config_path: str | None = Field(default=None, min_length=1)
    deadline_seconds: float = Field(gt=0)
    connect_timeout_seconds: float = Field(gt=0)
    read_timeout_seconds: float = Field(gt=0)
    write_timeout_seconds: float = Field(gt=0)
    pool_timeout_seconds: float = Field(gt=0)
    max_request_bytes: int = Field(gt=0)
    max_response_bytes: int = Field(gt=0)
    response_chunk_bytes: int = Field(gt=0)

    @field_validator("endpoint_ref")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        """Keep the configured URL intact and refuse embedded credentials."""
        url = urlsplit(value)
        # Accessing port also validates its numeric syntax and supported range.
        _ = url.port
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.fragment
            or any(character.isspace() for character in value)
        ):
            raise ValueError("endpoint_ref must be a full HTTP(S) service URL")
        return value

    @model_validator(mode="after")
    def validate_chunk_bound(self) -> ModelModelSwapServiceConfig:
        """A response chunk cannot exceed its declared total byte limit."""
        if self.response_chunk_bytes > self.max_response_bytes:
            raise ValueError("response_chunk_bytes exceeds max_response_bytes")
        return self


__all__ = ["ModelModelSwapServiceConfig"]
