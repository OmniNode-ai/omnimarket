# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev proof tenant token node's command and result.

The result names secret names and a status only: never a token, a key value or a signature.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumDevProofTokenOutcome(StrEnum):
    """The typed outcomes of a mint run."""

    MINTED = "minted"
    READY = "ready"
    REFUSED_NOT_DEV = "refused_not_dev"
    REFUSED_TENANT_NOT_ALLOWED = "refused_tenant_not_allowed"
    OVERLAY_UNAVAILABLE = "overlay_unavailable"
    SIGNING_KEY_UNAVAILABLE = "signing_key_unavailable"
    SIGNING_FAILED = "signing_failed"
    SEED_FAILED = "seed_failed"


class EnumDevProofTokenStatus(StrEnum):
    """What a run found or did for one token's secret name."""

    PRESENT = "present"
    ABSENT = "absent"
    WRITTEN = "written"
    FAILED = "failed"


class ModelDevProofTokenCommand(BaseModel):
    """Which lane acts, which tenants to mint and whether to write anything.

    ``lane`` left out means the process's ``ONEX_LANE``; either way it must be the contract's dev lane.
    ``tenants`` left out means every tenant the overlay declares. ``write`` false only reads: the key is read and each
    token's secret name is checked, and nothing is signed or written.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str | None = None
    tenants: tuple[str, ...] | None = None
    write: bool = False


class ModelDevProofTokenOutcome(BaseModel):
    """One token's secret name and its status; the value is never here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_slug: str
    secret_name: str
    status: EnumDevProofTokenStatus


class ModelDevProofTokenResult(BaseModel):
    """The typed result of a run; ``ok`` is true only for minted and ready."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    outcome: EnumDevProofTokenOutcome
    detail: str
    lane: str | None
    signing_key_name: str | None = None
    tokens: list[ModelDevProofTokenOutcome] = Field(default_factory=list)


__all__ = [
    "EnumDevProofTokenOutcome",
    "EnumDevProofTokenStatus",
    "ModelDevProofTokenCommand",
    "ModelDevProofTokenOutcome",
    "ModelDevProofTokenResult",
]
