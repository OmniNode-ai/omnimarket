# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests, deployment binding and terminal receipts of lane dispatch."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.lane_dispatch.model_lane_dispatch import ModelLaneDispatchPolicy


class ModelLaneDispatchOverlay(BaseModel):
    """The host supplies argv as data; no shell interpolation or inferred host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy: ModelLaneDispatchPolicy
    checker_command: tuple[str, ...] = Field(min_length=1)
    runner_command: tuple[str, ...] = Field(min_length=1)
    command_timeout_s: int = Field(gt=0)
    event_bus: Literal["inmemory", "kafka"]
    kafka_bootstrap: str | None
    bus_lane: str | None

    @model_validator(mode="after")
    def transport_binding(self) -> Self:
        if (self.event_bus == "kafka") != bool(self.kafka_bootstrap):
            raise ValueError("kafka needs a declared broker; inmemory has no broker")
        if (self.event_bus == "kafka") != bool(self.bus_lane):
            raise ValueError("kafka needs a declared lane; inmemory has no broker lane")
        return self


class ModelLaneDispatchRequest(BaseModel):
    """Serialized spec or hashed record, artifact destination and execution intent.

    A plain spec may omit policy: the effect stamps its authoritative overlay
    policy into the canonical record. An existing envelope must verify unchanged.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_json: str = Field(min_length=1)
    output_dir: str = Field(min_length=1)
    execute: bool = False


class ModelLaneDispatchResult(BaseModel):
    """A checker refusal carries no runnable argv. The hash identifies the job record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["checked", "submitted", "refused"]
    job_id: str = ""
    content_hash: str = ""
    record_path: str = ""
    brief_path: str = ""
    brief: str = ""
    runner_argv: tuple[str, ...] = ()
    error_code: str = ""
    detail: str = ""
    runner_exit: int | None = None
