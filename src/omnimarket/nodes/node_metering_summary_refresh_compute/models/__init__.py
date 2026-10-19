# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The part of a delegation terminal the metering refresh reads (OMN-17427)."""

from __future__ import annotations

from pydantic import AliasChoices, AwareDatetime, BaseModel, ConfigDict, Field


class ModelDelegationTerminalRefreshTrigger(BaseModel):
    """A delegation terminal, read for its run, tenant and time only.

    The terminals carry many more fields, which this node ignores; the
    delegation projection owns their meaning. The time aliases are the ones
    the delegate-skill terminal projection accepts.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    correlation_id: str = Field(..., min_length=1)
    tenant_id: str | None = Field(
        default=None, validation_alias=AliasChoices("tenant_id", "tenantId")
    )
    # None when the terminal names no time: it then asks for nothing, and the
    # schedule's whole refresh picks the run up.
    emitted_at: AwareDatetime | None = Field(
        default=None,
        validation_alias=AliasChoices("emitted_at", "emittedAt", "timestamp"),
    )


__all__: list[str] = ["ModelDelegationTerminalRefreshTrigger"]
