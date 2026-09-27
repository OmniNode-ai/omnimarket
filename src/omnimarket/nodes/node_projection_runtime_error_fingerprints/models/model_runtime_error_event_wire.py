# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire shape of one ``onex.evt.omnibase-infra.runtime-error.v1`` event.

A LOCAL wire model, not an import of omnibase_infra's
``ModelRuntimeErrorEvent``. Two reasons, both load-bearing:

* omnimarket must not take a runtime dependency on omnibase_infra to read a
  topic omnibase_infra happens to publish. The topic is the contract.
* the producer's model is ``extra="forbid"``; a reducer that refuses an event
  carrying a field the producer added last week converts a schema addition into
  silent data loss on the consuming side. This model tolerates unknown fields
  and states, per field, what it does with the ones it knows.

``error_category`` is accepted as a free string, never coerced to the reducer's
own enum here. It is the producer's *claim*, kept so the derivation can be
compared against it, and it is not what lands on the row.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelRuntimeErrorEventWire(BaseModel):
    """One runtime-error event as it arrives off the bus."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    event_id: str = Field(default="", description="Producer-assigned event id.")
    correlation_id: str = Field(
        default="",
        description=(
            "Trace correlation id. First-class here and on the row: it is what "
            "lets an error row hand a trace query to the trace surface."
        ),
    )

    logger_family: str = Field(
        default="", description="Python logger name that emitted the record."
    )
    log_level: str = Field(default="", description="ERROR / WARNING / CRITICAL.")

    message_template: str = Field(
        default="",
        description="Templatized message — variable parts already collapsed.",
    )
    raw_message: str = Field(default="", description="Original message.")

    error_category: str = Field(
        default="",
        description=(
            "The category the PRODUCER stamped. Retained as evidence of what "
            "the producer believed; never copied onto the row."
        ),
    )
    severity: str = Field(default="error", description="Producer-assigned severity.")

    fingerprint: str = Field(
        default="",
        description=(
            "The producer's fingerprint. Not used as the row key: it hashes the "
            "producer's own category, which is wrong for ~all lab rows."
        ),
    )
    occurrence_count_local: int = Field(
        default=1,
        ge=0,
        description="Occurrences the producer's rate limiter collapsed into this event.",
    )

    exception_type: str = Field(default="", description="Exception class name.")
    exception_message: str = Field(default="", description="Exception message.")
    stack_trace: str = Field(default="", description="Formatted traceback, if any.")

    hostname: str = Field(default="", description="Emitting host.")
    service_label: str = Field(default="", description="Emitting service.")

    emitted_at: datetime | None = Field(
        default=None,
        description="RuntimeLogEventBridge event time (current wire field).",
    )
    timestamp: datetime | None = Field(
        default=None,
        description="Legacy producer event time, retained for old events.",
    )

    @property
    def event_time(self) -> datetime | None:
        """Prefer the bridge's current event-time field over the legacy one."""
        return self.emitted_at or self.timestamp


__all__ = ["ModelRuntimeErrorEventWire"]
