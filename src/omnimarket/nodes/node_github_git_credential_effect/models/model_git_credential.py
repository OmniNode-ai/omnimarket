# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed I/O and policy of node_github_git_credential_effect (OMN-22613).

One request asks for a git credential for one repository. The node answers with
one issued event carrying the token sealed to the requester's key, or one failed
event with a typed reason. No model here ever holds a token in clear.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.github_credential_seal import MAX_PUBLIC_KEY_CHARS

GIT_CREDENTIAL_USERNAME: Final = "x-access-token"


class EnumGitCredentialRefusal(StrEnum):
    """Why no credential was issued."""

    REPOSITORY_MALFORMED = "repository_malformed"
    OWNER_NOT_ALLOWED = "owner_not_allowed"
    REPOSITORY_NOT_ALLOWED = "repository_not_allowed"
    RECIPIENT_KEY_MALFORMED = "recipient_key_malformed"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"
    MINT_REFUSED = "mint_refused"


class ModelGitCredentialRequest(BaseModel):
    """One requester asks for a push credential for one repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    repository: str = Field(
        min_length=3,
        max_length=200,
        description="OWNER/NAME; a trailing .git is accepted and dropped.",
    )
    recipient_public_key: str = Field(
        min_length=1,
        max_length=MAX_PUBLIC_KEY_CHARS,
        description="base64url of the requester's one-request X25519 public key.",
    )
    requested_by_lane: str | None = Field(default=None, max_length=200)
    requested_by_host: str | None = Field(default=None, max_length=200)


class ModelGitCredentialIssued(BaseModel):
    """The credential, sealed so only the requester opens it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["issued"] = "issued"
    correlation_id: UUID
    repository: str
    username: Literal["x-access-token"] = "x-access-token"
    sealed_token: str = Field(min_length=1)
    expires_at: datetime
    permissions: dict[str, str]
    requested_by_lane: str | None = None
    requested_by_host: str | None = None


class ModelGitCredentialFailed(BaseModel):
    """No credential was issued.

    ``terminal_failure_cause`` is the field the runtime's failure-terminal guard
    reads, so this result is published on the contract's failure terminal. It
    always equals ``reason``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["failed"] = "failed"
    correlation_id: UUID
    repository: str
    reason: EnumGitCredentialRefusal
    terminal_failure_cause: EnumGitCredentialRefusal | None = None
    detail: str = Field(min_length=1)
    requested_by_lane: str | None = None
    requested_by_host: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _cause_is_the_reason(cls, data: object) -> object:
        if isinstance(data, dict) and data.get("terminal_failure_cause") is None:
            return {**data, "terminal_failure_cause": data.get("reason")}
        return data

    @model_validator(mode="after")
    def _cause_matches(self) -> ModelGitCredentialFailed:
        if self.terminal_failure_cause is not self.reason:
            raise ValueError("terminal_failure_cause must equal reason")
        return self


class ModelGitCredentialPolicy(BaseModel):
    """The node's settings, read from its overlay (never hard-coded in the handler)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    git_host: str = Field(
        min_length=1,
        description="The host git asks the helper about; others get no answer.",
    )
    allowed_owners: tuple[str, ...] = Field(min_length=1)
    denied_repositories: tuple[str, ...] = ()
    permissions: dict[str, Literal["read", "write"]] = Field(min_length=1)
    app_id_secret: str = Field(min_length=1)
    private_key_secret: str = Field(min_length=1)
    installation_id_secret: str = Field(min_length=1)


__all__ = [
    "GIT_CREDENTIAL_USERNAME",
    "EnumGitCredentialRefusal",
    "ModelGitCredentialFailed",
    "ModelGitCredentialIssued",
    "ModelGitCredentialPolicy",
    "ModelGitCredentialRequest",
]
