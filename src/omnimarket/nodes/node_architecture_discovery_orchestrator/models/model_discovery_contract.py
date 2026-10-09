"""Validated discovery execution settings owned by contract.yaml."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelDiscoveryPhaseSpec(BaseModel):
    """One ordered phase and its packaged prompt."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    label: str
    phase: Literal["Scan", "Adjudicate", "Report"]
    model: str
    effort: str
    prompt: str


class ModelDiscoveryInvocation(BaseModel):
    """The reused invocation node and its contract-owned transport dependencies."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    node: str
    command_topic: str
    terminal_topic: str
    agent: Literal["claude"]
    sandbox: Literal["workspace_write"]
    timeout_ms: int = Field(gt=0)
    delivery_margin_seconds: int = Field(gt=0)


class ModelDiscoveryContract(BaseModel):
    """The subset of the canonical node contract consumed by coordination."""

    model_config = ConfigDict(frozen=True)
    invocation: ModelDiscoveryInvocation
    phases: tuple[ModelDiscoveryPhaseSpec, ...]
    runtime_dispatch: dict[str, str]
    event_bus: dict[str, tuple[str, ...]]
    terminal_event: str

    @field_validator("phases")
    @classmethod
    def complete_phase_graph(
        cls, value: tuple[ModelDiscoveryPhaseSpec, ...]
    ) -> tuple[ModelDiscoveryPhaseSpec, ...]:
        if tuple(p.phase for p in value) != ("Scan",) * 4 + ("Adjudicate", "Report"):
            raise ValueError(
                "discovery requires four scans, adjudication and report in that order"
            )
        if len({p.label for p in value}) != 6:
            raise ValueError("discovery phase labels must be unique")
        return value
