# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Result model for ``node_event_emit_effect`` (OMN-15965 R1)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelEmitResult(BaseModel):
    """Outcome of one ``handle()`` invocation: current publish + backlog drain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(..., min_length=1)
    topics_published: list[str] = Field(
        default_factory=list,
        description="Topics the current event was published to (empty if not published).",
    )
    published: bool = Field(
        ...,
        description="False in spool-only mode (no KAFKA_BOOTSTRAP_SERVERS) "
        "or when the current event's publish attempt failed. Use "
        "spool_only to distinguish the two cases.",
    )
    spool_only: bool = Field(
        default=False,
        description="True only when no publish adapter was configured "
        "(KAFKA_BOOTSTRAP_SERVERS unset) -- no publish was even attempted. "
        "False whenever a publish attempt occurred, whether it succeeded "
        "(published=True) or failed (published=False). Distinguishes "
        "'no adapter configured' from 'publish attempted and failed' "
        "(OMN-15987 finding 1).",
    )
    drained_count: int = Field(
        default=0,
        ge=0,
        description="Backlog records also published (and acked) this invocation.",
    )
    dropped_count: int = Field(
        default=0,
        ge=0,
        description="Telemetry-tier records dropped this invocation due to "
        "bounded-spool overflow.",
    )
    correlation_id: str | None = Field(default=None)


__all__: list[str] = ["ModelEmitResult"]
