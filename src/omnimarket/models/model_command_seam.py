# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared facts and readings for the standing command-publication census."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_command_invocation import ModelCommandInvocation


class ModelCommandSeamOverlay(BaseModel):
    """Deployment scope supplied by the installer; no host is built into code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str = Field(min_length=1)
    manifest_urls: tuple[str, ...] = Field(min_length=1)
    projection_url: str = Field(min_length=1)
    max_projection_pages: int = Field(gt=0, le=64)
    package_roots: dict[str, str] = Field(min_length=1)
    root_topics: tuple[str, ...] = Field(min_length=1)
    max_age_seconds: int = Field(gt=0)
    manifest_refresh_seconds: int = Field(gt=0)
    request_timeout_seconds: float = Field(gt=0)
    max_facts_per_event: int = Field(gt=0, le=100)


class ModelCommandSeamNode(BaseModel):
    """One loaded contract, joined to hash-matched installed invocation data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str
    package: str
    subscribes: tuple[str, ...] = ()
    publishes: tuple[str, ...] = ()
    command_topic: str | None = None
    invocation: ModelCommandInvocation | None = None
    external_senders: dict[str, str] = Field(default_factory=dict)
    binding_error: str | None = None


class ModelCommandSeamExternalSender(BaseModel):
    """An older named external producer does not declare its invocation mode."""

    model_config = ConfigDict(frozen=True, extra="ignore", str_strip_whitespace=True)

    topic: str = Field(min_length=1)
    producer: str = Field(min_length=1)


class ModelCommandSeamFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str
    package: str
    topic: str
    invocation: ModelCommandInvocation | None = None
    contract_senders: tuple[str, ...] = ()
    automatic_senders: tuple[str, ...] = ()
    external_sender_declarations: tuple[str, ...] = ()
    binding_error: str | None = None
    topic_observed: bool
    high_watermark: int | None = None
    low_watermark: int | None = None
    last_crossed_at: datetime | None = None
    last_crossed_source: Literal["sampler", "projection_last_known", "unmeasured"] = (
        "unmeasured"
    )


class ModelCommandSeamFacts(BaseModel):
    """A bounded part, published on the declared facts route to the compute node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: Literal["command-seam-facts"] = "command-seam-facts"
    scope: str
    sample_id: str
    sampled_at: datetime
    read_at: datetime
    max_age_seconds: int = Field(gt=0)
    part_index: int = Field(ge=0)
    part_count: int = Field(gt=0)
    inventory_count: int = Field(ge=0)
    facts: tuple[ModelCommandSeamFact, ...] = ()
    errors: tuple[str, ...] = ()


class ModelCommandSeamReading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str
    package: str
    topic: str
    state: Literal["WORKING", "NOT_WORKING", "ON_DEMAND_IDLE", "UNKNOWN"]
    reason: str
    automatic_senders: tuple[str, ...]
    declared_sender: str | None
    last_crossed_at: datetime | None
    last_crossed_source: Literal["sampler", "projection_last_known", "unmeasured"]
    high_watermark: int | None
    low_watermark: int | None
    # Publication is the measured seam. Handler execution, durable results and
    # negative seven-day coverage cannot be inferred from a broker timestamp.
    execution_evidence: Literal["unmeasured"] = "unmeasured"
    seven_day_absence_evidence: Literal["unmeasured"] = "unmeasured"


class ModelCommandSeamReadings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: Literal["command-seam-readings"] = "command-seam-readings"
    scope: str
    sample_id: str
    sampled_at: datetime
    read_at: datetime
    valid_until: datetime
    part_index: int
    part_count: int
    inventory_count: int
    readings: tuple[ModelCommandSeamReading, ...]
    errors: tuple[str, ...]
