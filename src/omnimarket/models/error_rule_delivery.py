# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delivery ports shared by the hook edge, matcher and silent-failure alarm."""

from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Surface = Literal["tool_failure", "guard_refusal"]


class ModelDeliveryFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    surface: Surface
    session_id: str = Field(min_length=1)
    tool_use_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    error_text: str
    hook_event_name: Literal["PostToolUseFailure", "PostToolUse", "PreToolUse"] = (
        "PostToolUseFailure"
    )
    probe_id: str | None = None


class ModelDeliveryMatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["MATCH", "NO-MATCH", "UNREAD"]
    class_key: str = ""
    line: str = ""
    detail: str = ""


class ModelDeliveryBinding(BaseModel):
    """A server overlay binds a no-argument factory; bus callers cannot name code."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    module: str
    factory: str


class ModelDeliveryPathCommand(BaseModel):
    """The installed hook/refusal entrypoint, declared by its host overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    command: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)


class ModelDeliveryOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    state_dir: Path
    exception_dir: Path
    mode: Literal["hint", "shadow"]
    queue_context: bool
    window_seconds: float = Field(gt=0)
    probe_interval_seconds: float = Field(gt=0)
    probe_timeout_seconds: float = Field(gt=0)
    latency_budget_ms: float = Field(gt=0)
    exception_class: str
    probes: tuple[ModelDeliveryFailure, ...]
    matcher: ModelDeliveryBinding | None = None
    delivery_paths: dict[Surface, ModelDeliveryPathCommand] = Field(
        default_factory=dict
    )


class ModelDeliveryRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    operation: Literal["failure", "pre_tool", "traffic", "tick", "drain"]
    now: AwareDatetime
    failure: ModelDeliveryFailure | None = None
    session_id: str = ""
    traffic_surface: Surface | None = None


class ModelDeliveryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: str
    line: str = ""
    hook_output: dict[str, dict[str, str]] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    acknowledged: int = 0
    matcher_calls: dict[Surface, int] = Field(default_factory=dict)
    traffic: dict[Surface, int] = Field(default_factory=dict)


class ModelDeliverySignal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    surface: Surface
    cause: Literal[
        "UNREAD", "probe-not-seen", "zero-matcher-calls", "over-budget", "outbox-unread"
    ]
    detail: str


class ModelDeliveryAlarmRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    operation: Literal["delivery-health"] = "delivery-health"
    now: AwareDatetime
    signals: tuple[ModelDeliverySignal, ...]
    apply: bool = True


class ModelDeliveryAlarmResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    raised: bool
    rows: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class ModelDeliveryJournalEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal[
        "traffic", "matcher", "handled", "outbox", "ack", "delivered", "probe"
    ]
    key: str
    at: AwareDatetime
    surface: Surface
    failure: ModelDeliveryFailure | None = None
    match: ModelDeliveryMatch | None = None
    queued: bool = False
