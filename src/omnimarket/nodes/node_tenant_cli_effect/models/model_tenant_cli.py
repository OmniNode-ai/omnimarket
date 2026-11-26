# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests and the one result of the ``onex tenant`` commands.

The result carries a typed ``outcome`` string that is either the answer of the node the command asked or a refusal of the
command itself (``refused_*``, ``no_answer``). ``plaintext`` is the one field that holds a minted key: it is a
``SecretStr`` excluded from serialization and from the repr, so it reaches only the command's single stdout line.
"""

from __future__ import annotations

from enum import IntEnum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr


class EnumTenantCliExit(IntEnum):
    """The process exit code of a command."""

    OK = 0
    REQUEST_TO_FIX = 2
    NODE_OR_BUS_PROBLEM = 3


class ModelTenantCreateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_slug: str = Field(min_length=1)
    name: str | None = None


class ModelTenantShowRequest(BaseModel):
    """Exactly one of the two; the handler refuses anything else."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID | None = None
    tenant_slug: str | None = None


class ModelTenantKeyMintRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    name: str = Field(min_length=1)
    expires_at: AwareDatetime | None = None
    store_dev_secret: bool = False


class ModelTenantKeyRevokeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    credential_id: UUID


class ModelTenantCliResult(BaseModel):
    """What a command prints: the outcome, whether it succeeded, a detail line and the answer's fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    outcome: str
    ok: bool
    detail: str
    exit_code: EnumTenantCliExit
    fields: dict[str, str] = Field(default_factory=dict)
    #: The minted key. Never serialized and never in a repr: the command prints it once.
    plaintext: SecretStr | None = Field(default=None, exclude=True, repr=False)


__all__: list[str] = [
    "EnumTenantCliExit",
    "ModelTenantCliResult",
    "ModelTenantCreateRequest",
    "ModelTenantKeyMintRequest",
    "ModelTenantKeyRevokeRequest",
    "ModelTenantShowRequest",
]
