# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev identity provision node's command and result (OMN-24115).

The result carries names and ids only: never a token, a secret or a password, and never the response's
``bootstrap_token``.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator


class EnumDevIdentityOutcome(StrEnum):
    """The typed outcomes of a dev identity provision run."""

    PROVISIONED = "provisioned"
    READY = "ready"
    REFUSED_NOT_DEV = "refused_not_dev"
    ADMIN_CREDENTIAL_MISSING = "admin_credential_missing"
    ADMIN_TOKEN_REFUSED = "admin_token_refused"
    KEYCLOAK_UNREACHABLE = "keycloak_unreachable"
    PASSWORD_UNAVAILABLE = "password_unavailable"
    USER_CREATE_FAILED = "user_create_failed"
    USER_TOKEN_REFUSED = "user_token_refused"
    PROVISION_REFUSED = "provision_refused"
    RUNTIME_UNREACHABLE = "runtime_unreachable"
    PROVISION_RESPONSE_INVALID = "provision_response_invalid"
    INVALID_IDENTITY_INPUT = "invalid_identity_input"
    USER_PROTECTED = "user_protected"
    USER_EXISTS_REFUSED = "user_exists_refused"
    USER_FOUND = "user_found"
    USER_ABSENT = "user_absent"
    USER_DELETED = "user_deleted"
    USER_DELETE_FAILED = "user_delete_failed"


class EnumDevIdentityCleanup(StrEnum):
    """What a run did about the user it created, once it ended."""

    NOT_CREATED = "not_created"
    DELETED = "deleted"
    DELETE_FAILED = "delete_failed"
    KEPT = "kept"
    KEPT_PROTECTED = "kept_protected"


class ModelDevIdentityProvisionCommand(BaseModel):
    """The command a run or probe carries.

    ``username``, ``email`` and ``password_key_id`` name the identity to provision; each left out is the overlay user's
    value. A username other than the overlay's must carry its own email and password key id, so a new user never borrows
    the overlay user's. An existing user is never given a new password unless ``reset_existing`` is set, and a username in
    the overlay's ``protected_usernames`` is refused unless ``allow_protected`` is set. The tenant is created by the
    runtime's tenant provision command, named from the username, so the command carries no tenant name.

    A run deletes the user it created when it ends in any typed failure, and after a success only when ``disposable``
    is set; a user that existed before the run, and a protected username, is never deleted by a run. ``lookup_user``
    reads one named user (``write`` false) and ``delete_user`` deletes one named user and reads it gone (``write``
    true); both need ``username``, exclude each other, and a delete refuses the overlay user and every protected
    username.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    write: bool
    username: str | None = None
    email: str | None = None
    password_key_id: str | None = None
    reset_existing: bool = False
    allow_protected: bool = False
    disposable: bool = False
    lookup_user: bool = False
    delete_user: bool = False

    @model_validator(mode="after")
    def _user_verbs_name_one_user(self) -> ModelDevIdentityProvisionCommand:
        if self.lookup_user and self.delete_user:
            raise ValueError("lookup_user and delete_user exclude each other")
        if (self.lookup_user or self.delete_user) and not self.username:
            raise ValueError("lookup_user and delete_user need a username")
        if self.lookup_user and self.write:
            raise ValueError("lookup_user reads only: write must be false")
        if self.delete_user and not self.write:
            raise ValueError("delete_user writes: write must be true")
        return self


class ModelDevIdentityProvisionResult(BaseModel):
    """The typed result of a run; ``ok`` is true only for provisioned and ready."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    outcome: EnumDevIdentityOutcome
    detail: str
    realm: str
    lane: str
    user_name: str | None = None
    tenant_id: str | None = None
    tenant_slug: str | None = None
    user_id: str | None = None
    cleanup: EnumDevIdentityCleanup | None = None
    expected_secret: str | None = None


__all__ = [
    "EnumDevIdentityCleanup",
    "EnumDevIdentityOutcome",
    "ModelDevIdentityProvisionCommand",
    "ModelDevIdentityProvisionResult",
]
