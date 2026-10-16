# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request for the tenant key pool expiry alert compute."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_key_pool_expiry_alert_compute.models.model_key_pool_credential_row import (
    ModelKeyPoolCredentialRow,
)

DEFAULT_WARNING_WINDOW_DAYS = 30


class ModelKeyPoolExpiryAlertRequest(BaseModel):
    """One tenant's credential rows and the clock to judge them by."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(..., min_length=1)
    credentials: tuple[ModelKeyPoolCredentialRow, ...] = Field(
        ..., description="Every catalog row of this tenant, live or not."
    )
    now: AwareDatetime = Field(
        ..., description="The evaluation instant; the handler never reads a clock."
    )
    warning_window_days: int = Field(default=DEFAULT_WARNING_WINDOW_DAYS, ge=1)
    channel: str = Field(
        ...,
        min_length=1,
        description="Slack channel id supplied by the caller from overlay config.",
    )
    correlation_id: UUID

    @model_validator(mode="after")
    def _rows_belong_to_the_tenant(self) -> Self:
        foreign = sorted(
            {
                row.api_key_ref
                for row in self.credentials
                if row.tenant_id != self.tenant_id
            }
        )
        if foreign:
            raise ValueError(
                f"credential rows of another tenant refused for {self.tenant_id}: "
                f"{', '.join(foreign)}"
            )
        return self


__all__ = ["DEFAULT_WARNING_WINDOW_DAYS", "ModelKeyPoolExpiryAlertRequest"]
