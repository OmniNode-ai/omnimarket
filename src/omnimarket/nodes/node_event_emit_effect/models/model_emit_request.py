# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for ``node_event_emit_effect`` (OMN-15965 R1)."""

from __future__ import annotations

import re
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

JsonType = dict[str, object] | list[object] | str | int | float | bool | None

# Same shape the static contract-topic-graph enforces
# (src/omnimarket/validators/contract_topic_graph.py's _TOPIC_RE) -- this is
# a format check only, not a registry-membership check. ``topic`` is a
# deliberate escape hatch (see field description below), so it is not
# restricted to registry-declared topics; it must still be a well-formed
# ONEX topic string, not an arbitrary value.
_TOPIC_SHAPE_RE = re.compile(r"^onex\.(evt|cmd|intent|dlq)\.[a-z0-9._-]+\.v\d+$")


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
            "Explicit topic override, publishing to exactly this one topic "
            "instead of event_type's resolved fan_out set. This is a "
            "deliberate escape hatch OUTSIDE contract.yaml's declared "
            "publish_topics / the event registry -- it is not validated "
            "against either, only against the well-formed ONEX topic shape "
            "(onex.{evt|cmd|intent|dlq}.<service>.<name>.vN). Callers "
            "reaching for this to route around a missing registry entry "
            "should register the topic properly instead; it exists for "
            "cases that are legitimately outside the registry's scope."
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

    @field_validator("topic")
    @classmethod
    def _topic_must_be_well_formed(cls, value: str | None) -> str | None:
        if value is not None and not _TOPIC_SHAPE_RE.match(value):
            raise ValueError(
                f"topic override {value!r} is not a well-formed ONEX topic "
                "(expected onex.{evt|cmd|intent|dlq}.<service>.<name>.vN)"
            )
        return value


__all__: list[str] = ["JsonType", "ModelEmitRequest"]
