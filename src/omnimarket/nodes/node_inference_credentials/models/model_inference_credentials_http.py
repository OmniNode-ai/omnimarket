# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and response of the inference credentials handler."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, SecretBytes


class EnumInferenceCredentialsOperation(StrEnum):
    """The three operations the routes carry."""

    CREATE = "create"
    LIST = "list"
    REVOKE = "revoke"


class ModelInferenceCredentialsRequest(BaseModel):
    """What the mount hands the handler: headers for the identity resolver and the input.

    ``body`` is the raw create body held as a secret: it carries the customer's key
    value, so it never prints in a repr or a log line and is read once, by the handler,
    to validate it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumInferenceCredentialsOperation
    headers: dict[str, str] = Field(default_factory=dict)
    body: SecretBytes | None = None
    api_key_ref: str | None = None


class ModelInferenceCredentialsResponse(BaseModel):
    """Status code and JSON body the mount answers with."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status_code: int
    body: dict[str, object] | list[dict[str, object]]


__all__: list[str] = [
    "EnumInferenceCredentialsOperation",
    "ModelInferenceCredentialsRequest",
    "ModelInferenceCredentialsResponse",
]
