# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev signed-in read node's command and result (OMN-24345).

The result carries names, a status and a data state only: never a token, a password or a row of the answer.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumDevSignedInReadOutcome(StrEnum):
    """The typed outcomes of a dev signed-in read run."""

    READ_ACCEPTED = "read_accepted"
    READY = "ready"
    REFUSED_NOT_DEV = "refused_not_dev"
    PASSWORD_MISSING = "password_missing"
    KEYCLOAK_UNREACHABLE = "keycloak_unreachable"
    USER_TOKEN_REFUSED = "user_token_refused"
    API_UNREACHABLE = "api_unreachable"
    READ_UNAUTHORIZED = "read_unauthorized"
    READ_REFUSED = "read_refused"
    READ_RESPONSE_INVALID = "read_response_invalid"


class ModelDevSignedInReadCommand(BaseModel):
    """The command a run or probe carries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    write: bool


class ModelDevSignedInReadResult(BaseModel):
    """The typed result of a run; ``ok`` is true only for read_accepted and ready."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    outcome: EnumDevSignedInReadOutcome
    detail: str
    realm: str
    lane: str
    user_name: str | None = None
    http_status: int | None = None
    data_state: str | None = None
    expected_secret: str | None = None


__all__ = [
    "EnumDevSignedInReadOutcome",
    "ModelDevSignedInReadCommand",
    "ModelDevSignedInReadResult",
]
