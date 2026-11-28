# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The settings store provision node's command and result (OMN-24052). Names and outcomes only: never a password."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumSettingsStoreLogin(StrEnum):
    """The three roles of the settings store (valkey_acl.py): the contract says which of them a run may apply."""

    WRITER = "writer"
    READER = "reader"
    CACHE = "cache"


class EnumSettingsStoreOutcome(StrEnum):
    """The typed outcomes of one run; ``provisioned`` and ``already_provisioned`` are the only ok ones."""

    PROVISIONED = "provisioned"
    ALREADY_PROVISIONED = "already_provisioned"
    REFUSED_LANE_NOT_ALLOWED = "refused_lane_not_allowed"
    REFUSED_LOGIN_NOT_DECLARED = "refused_login_not_declared"
    REFUSED_DATABASE_IN_USE = "refused_database_in_use"
    REFUSED_LOGIN_HOLDS_OTHER_PASSWORD = "refused_login_holds_other_password"
    PASSWORD_UNAVAILABLE = "password_unavailable"
    ADMIN_UNAVAILABLE = "admin_unavailable"
    STORE_UNAVAILABLE = "store_unavailable"
    NOT_VERIFIED = "not_verified"


class ModelSettingsStoreProvisionCommand(BaseModel):
    """Which lane acts and which logins to apply; the contract decides which logins a run may name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    logins: tuple[EnumSettingsStoreLogin, ...] = (EnumSettingsStoreLogin.WRITER,)


class ModelSettingsStoreProvisionResult(BaseModel):
    """The outcome, the lane, the login names acted on and the password's secret folder and key name; never a value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    outcome: EnumSettingsStoreOutcome
    detail: str
    lane: str
    applied: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    secret_folder: str | None = None
    secret_key: str | None = None


__all__ = [
    "EnumSettingsStoreLogin",
    "EnumSettingsStoreOutcome",
    "ModelSettingsStoreProvisionCommand",
    "ModelSettingsStoreProvisionResult",
]
