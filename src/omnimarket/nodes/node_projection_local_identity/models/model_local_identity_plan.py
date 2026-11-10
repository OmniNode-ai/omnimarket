# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The plan's typed input and output: one mint event and its tenant's stored row in, a row out."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.models.model_local_identity_minted import ModelLocalIdentityMinted


class ModelLocalIdentityPlanRequest(BaseModel):
    """A mint event, the tenant's stored rows and the write stamp."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event: ModelLocalIdentityMinted
    stored_rows: tuple[dict[str, object], ...] = ()
    updated_at: datetime


class ModelLocalIdentityPlanResponse(BaseModel):
    """The row to write; ``None`` when the stored row already holds the earliest mint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: dict[str, object] | None = None


__all__ = ["ModelLocalIdentityPlanRequest", "ModelLocalIdentityPlanResponse"]
