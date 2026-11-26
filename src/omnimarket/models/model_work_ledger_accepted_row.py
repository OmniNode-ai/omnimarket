# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One row of the accepted-row topic, shared by every node that reads or publishes it (OMN-25630).

The accepted-row topic carries every row the judge accepted, once, with its sequence. History up to
the watermark is published once marked ``bootstrap``; live rows follow marked ``live``. The projection,
the render node, the in-memory reader, the inbox spool, the bootstrap relay and the republish command
all read this one envelope, so it lives here and in no node's private package.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_work_ledger_projection_inbound import (
    ModelWorkLedgerProjectionInbound,
)


class EnumAcceptedRowSource(StrEnum):
    BOOTSTRAP = "bootstrap"
    LIVE = "live"


class ModelAcceptedRowEvent(BaseModel):
    """One accepted row as the accepted-row topic carries it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    ledger_id: str = Field(min_length=1)
    row_id: str = Field(pattern=r"^[0-9a-f]{64}$", description="sha256 of raw_row")
    raw_row: str = Field(min_length=1)
    source: EnumAcceptedRowSource
    ledger_seq: int | None = Field(default=None, ge=1)
    legacy_ordinal: int | None = Field(default=None, ge=1)
    req: str | None = Field(
        default=None, description="The command's req= cell, when it had one."
    )
    typed: ModelWorkLedgerProjectionInbound | None = Field(
        default=None,
        description=(
            "The typed envelope the row was first emitted as, when it was one; the republish "
            "command sends it back to the typed row topic of its type."
        ),
    )


__all__: list[str] = ["EnumAcceptedRowSource", "ModelAcceptedRowEvent"]
