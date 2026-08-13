# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for ``node_event_emit_effect`` (OMN-15965 R1)."""

from __future__ import annotations

from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

JsonType = dict[str, object] | list[object] | str | int | float | bool | None


class ModelEmitRequest(BaseModel):
    """A single event submitted to ``node_event_emit_effect`` for publishing.

    ``event_type`` is looked up against ``registries/topics.yaml``
    ``events.<event_type>.fan_out``; an unknown ``event_type`` fails fast --
    there is no silent default topic.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: str = Field(
        ..., min_length=1, description="Semantic event type (e.g. 'session.started')"
    )
    payload: JsonType = Field(default_factory=dict, description="Event payload")
    correlation_id: str | None = Field(
        default=None, description="Correlation ID for tracing"
    )
    topic: str | None = Field(
        default=None,
        description=(
            "Explicit topic override. If unset, the topic(s) are resolved "
            "from the event registry via event_type."
        ),
    )
    partition_key: str | None = Field(
        default=None, description="Kafka partition key override"
    )
    event_id: str = Field(
        default_factory=lambda: str(uuid4()),
        min_length=1,
        description="Unique event identifier; stable across spool retries.",
    )


__all__: list[str] = ["JsonType", "ModelEmitRequest"]
