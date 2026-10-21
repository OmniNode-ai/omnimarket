# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shared-lane metering refresh command (OMN-17427).

``node_metering_summary_refresh_compute`` produces it, on a delegation terminal
and on its runtime-tick schedule; ``node_projection_metering_summary`` consumes
it, reads the tenant's delegation_events and folds them into metering_summary.

The command names the tenants and days to refresh, never the runs. A fold
request carries every recorded run of a tenant, about 244 bytes each, so a
tenant past roughly 4,300 runs cannot fit the bus's 1 MiB message limit; the
dev lane's 47,751 runs would be an 11.7 MB message. The records are read where
the fold is written instead, so the bus carries only this small command.

Shared because two nodes speak it, so it lives in omnimarket.models and in
neither node's models package.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class EnumMeteringRefreshReason(StrEnum):
    """Why a refresh was asked for."""

    DELEGATION_TERMINAL = "delegation_terminal"
    SCHEDULE = "schedule"
    BACKFILL = "backfill"


class ModelMeteringSummaryRefreshRequested(BaseModel):
    """Refresh the stored metering rows of some or all tenants from their runs.

    ``tenant_ids`` None means every tenant the tenant registry mirror holds.
    ``days`` None means every recorded UTC day plus the all-time row; a set
    names the days to refold, and the all-time row is always refolded. A
    tenant with no stored all-time row under the current baseline and pricing
    manifest is refolded whole whatever ``days`` says.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    refresh_id: str = Field(..., min_length=1)
    reason: EnumMeteringRefreshReason
    tenant_ids: tuple[str, ...] | None = None
    days: frozenset[date] | None = None
    requested_at: AwareDatetime

    @field_validator("tenant_ids")
    @classmethod
    def _tenants_named(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and (not value or any(not t.strip() for t in value)):
            raise ValueError("tenant_ids, when given, must name at least one tenant")
        return value


__all__: list[str] = [
    "EnumMeteringRefreshReason",
    "ModelMeteringSummaryRefreshRequested",
]
