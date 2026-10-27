# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tolerant of receipt metadata; strict about any measurements used."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from .model_work_attempt_receipt import Amount, Count, Unknown


class ModelSourceUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    input_tokens: Count | Unknown | None = None
    output_tokens: Count | Unknown | None = None
    cache_read_input_tokens: Count | Unknown | None = None
    cache_creation_input_tokens: Count | Unknown | None = None
    num_turns: Count | Unknown | None = None
    cost_usd: Amount | Unknown | None = None
    cost_measured: bool | None = None


class ModelDelegateAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    acceptance_decision: str | None = None
    acceptance_reason: str | None = None
    input_tokens_measured: Count | Unknown | None = None
    cost_usd: Amount | Unknown | None = None
    usage_source: str | None = None


class ModelSourceTelemetry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    tool_calls: Count | Unknown | None = None


class ModelRubricTelemetry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    measured: ModelSourceTelemetry | None = None


class ModelSourceConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    effort: str | None = None


class ModelReceiptResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    metrics: ModelSourceUsage | None = None


class ModelNestedReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    result: ModelReceiptResult | None = None


class ModelReceiptDocument(ModelSourceUsage):
    engine: str | None = None
    model: str | None = None
    effort: str | None = None
    contract_line: str | None = None
    attempt_line: str | None = None
    contract_id: str | None = None
    attempt_id: str | None = None
    correlation_id: UUID | None = None
    lease_id: str | None = None
    tokens_in: Count | Unknown | None = None
    tokens_out: Count | Unknown | None = None
    cache_read: Count | Unknown | None = None
    cache_write: Count | Unknown | None = None
    wall_s: Amount | Unknown | None = None
    duration_s: Amount | Unknown | None = None
    duration_ms: Amount | Unknown | None = None
    turns: Count | Unknown | None = None
    tool_calls: Count | Unknown | None = None
    retries: Count | Unknown | None = None
    total_cost_usd: Amount | Unknown | None = None
    usage_source: str | None = None
    usage: ModelSourceUsage | None = None
    metrics: ModelSourceUsage | None = None
    measured: ModelSourceTelemetry | None = None
    rubric_verdict: ModelRubricTelemetry | None = None
    receipt: ModelNestedReceipt | None = None
    config: ModelSourceConfig | None = None
    attempts: tuple[ModelDelegateAttempt, ...] = ()
