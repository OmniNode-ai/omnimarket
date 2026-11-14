# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The wire models the dev identity provision transport moves (OMN-24115).

Every field that can hold a secret is excluded from dumps and reprs: a request or response that lands in a log or a
result must not carry a bearer, a form secret or a body secret.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ModelIdentityHttpRequest(BaseModel):
    """One request the handler asks the transport to send; secrets are excluded from every dump."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal["GET", "POST", "PUT"]
    url: str
    query: dict[str, str] = Field(default_factory=dict)
    bearer: SecretStr | None = Field(default=None, exclude=True, repr=False)
    form: dict[str, SecretStr] | None = Field(default=None, exclude=True, repr=False)
    json_body: dict[str, Any] | None = Field(default=None, exclude=True, repr=False)
    timeout_seconds: float


class ModelIdentityHttpResponse(BaseModel):
    """The transport's answer: a status, headers and a decoded body; the body is excluded from dumps."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: int
    headers: dict[str, str] = Field(default_factory=dict)
    json_body: dict[str, Any] | list[Any] | None = Field(
        default=None, exclude=True, repr=False
    )


__all__ = ["ModelIdentityHttpRequest", "ModelIdentityHttpResponse"]
