# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stability gateway key provision node's command and result (OMN-26586).

The result carries names and ids only: never the key value. It has no field that can hold one.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class EnumStabilityKeyOutcome(StrEnum):
    """The typed outcomes of one run; ``provisioned`` and ``already_provisioned`` are the only ok ones."""

    PROVISIONED = "provisioned"
    ALREADY_PROVISIONED = "already_provisioned"
    REFUSED_LANE_NOT_ALLOWED = "refused_lane_not_allowed"
    REFUSED_CHECK_NOT_DECLARED = "refused_check_not_declared"
    KEY_UNAVAILABLE = "key_unavailable"
    TENANT_REFUSED = "tenant_refused"
    KEY_NAME_CONFLICT = "key_name_conflict"
    KEY_HASH_TAKEN = "key_hash_taken"
    STORE_UNAVAILABLE = "store_unavailable"


class EnumTenantEnsure(StrEnum):
    """What ensuring the tenant row found and did."""

    CREATED = "created"
    EXISTS = "exists"
    REFUSED = "refused"


class EnumApiKeyEnsure(StrEnum):
    """What ensuring the key row found and did."""

    CREATED = "created"
    EXISTS = "exists"
    NAME_CONFLICT = "name_conflict"
    HASH_TAKEN = "hash_taken"


class ModelTenantEnsured(BaseModel):
    """The tenant the identity store holds for the slug, or why it holds none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumTenantEnsure
    tenant_id: UUID | None = None
    detail: str = ""


class ModelStabilityKeyProvisionCommand(BaseModel):
    """The command: which lane acts, and for which check. The tenant slug, the key name and the key id are the contract's.

    ``check`` names one entry of the contract's ``stability_only.checks`` block, so each check stores its own key under its own
    name. Left out, the run is the default run: the key the stability probes already read, exactly as before.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    check: str | None = None


class ModelStabilityKeyProvisionResult(BaseModel):
    """The typed result of a run; ``ok`` is true only for provisioned and already_provisioned."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    outcome: EnumStabilityKeyOutcome
    detail: str
    lane: str
    tenant_id: str | None = None
    tenant_slug: str | None = None
    key_name: str | None = None
    secret_folder: str | None = None
    secret_key: str | None = None


__all__ = [
    "EnumApiKeyEnsure",
    "EnumStabilityKeyOutcome",
    "EnumTenantEnsure",
    "ModelStabilityKeyProvisionCommand",
    "ModelStabilityKeyProvisionResult",
    "ModelTenantEnsured",
]
