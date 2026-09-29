# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and durable rows for the metering summary projection."""

from datetime import date, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_metering_summary_compute import (
    EnumBaselineState,
    ModelCounterfactualBaseline,
    ModelMeteringRecord,
)


class ModelMeteringSummaryRow(BaseModel):
    """A replaceable snapshot; money is decimal text, unknown money is null."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: str = Field(min_length=1)
    window_kind: Literal["day", "all"]
    window_start: str
    window_end: str
    as_of: str
    baseline_model: str = Field(min_length=1)
    pricing_manifest_version: str | None
    baseline_state: EnumBaselineState
    runs_total: int
    runs_measured: int
    runs_unknown_tokens: int
    runs_unknown_spend: int
    tokens_in: int
    tokens_out: int
    spend_usd: str | None
    counterfactual_usd: str | None
    savings_usd: str | None
    summary_json: str

    @model_validator(mode="after")
    def validate_window(self) -> "ModelMeteringSummaryRow":
        if self.window_kind == "all":
            if self.window_start != "":
                raise ValueError("all window_start must be empty")
        elif date.fromisoformat(self.window_start).isoformat() != self.window_start:
            raise ValueError("day window_start must be an ISO date")
        datetime.fromisoformat(self.window_end)
        datetime.fromisoformat(self.as_of)
        return self


class ModelMeteringSummaryFoldRequest(BaseModel):
    """All inputs are explicit, including the clock and resolved price."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: str = Field(min_length=1)
    records: tuple[ModelMeteringRecord, ...] = ()
    baseline: ModelCounterfactualBaseline | None = None
    baseline_model: str = Field(min_length=1)
    as_of: AwareDatetime
    days: frozenset[date] | None = None

    @model_validator(mode="after")
    def validate_baseline(self) -> "ModelMeteringSummaryFoldRequest":
        if self.baseline is not None and self.baseline.model != self.baseline_model:
            raise ValueError("resolved baseline must match the requested model")
        if any(r.occurred_at.utcoffset() is None for r in self.records):
            raise ValueError("record timestamps must be timezone aware")
        return self


class ModelMeteringSummaryFoldResult(BaseModel):
    """Rows in deterministic (window_kind, window_start) order."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    rows: tuple[ModelMeteringSummaryRow, ...]
