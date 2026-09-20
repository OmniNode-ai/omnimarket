# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One ranked fingerprint row — the unit the Lab Errors widget renders."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_runtime_error_fingerprints.models.enum_runtime_error_category import (
    EnumRuntimeErrorCategory,
)
from omnimarket.nodes.node_projection_runtime_error_fingerprints.models.enum_runtime_error_severity import (
    EnumRuntimeErrorSeverity,
)


class ModelRuntimeErrorFingerprintRow(BaseModel):
    """A single distinct runtime error, with everything seen of it so far.

    The point of the row is that a flood of identical traces is ONE of these
    with a high ``occurrence_count``, not N of them — that collapse is the one
    genuinely portable idea in the archived runtime-errors view, and it is the
    reason the exposure can be truncated to a page and still be useful.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fingerprint: str = Field(..., description="Stable identity of this error class.")
    logger_name: str = Field(..., description="Emitting logger.")
    error_category: EnumRuntimeErrorCategory = Field(
        ..., description="Subsystem, DERIVED here from the event's own evidence."
    )
    severity: EnumRuntimeErrorSeverity = Field(..., description="Severity.")
    message_template: str = Field(..., description="Templatized message.")
    exception_type: str = Field(default="", description="Exception class, if any.")

    occurrence_count: int = Field(
        ..., ge=0, description="Total occurrences seen for this fingerprint."
    )

    correlation_id: str = Field(
        default="",
        description=(
            "Correlation id of the MOST RECENT occurrence. A fingerprint spans "
            "many traces; the most recent is the one an operator clicking "
            "through wants, and it is the one that is still in the trace "
            "surface's retention window."
        ),
    )

    service_name: str = Field(default="", description="Emitting service.")
    hostname: str = Field(default="", description="Emitting host.")

    first_seen_at: datetime = Field(..., description="Earliest occurrence observed.")
    last_seen_at: datetime = Field(..., description="Most recent occurrence observed.")

    category_evidence: str = Field(
        ...,
        description=(
            "WHICH rule produced error_category — exception_type, "
            "logger_prefix, message_keyword, or none. A category with no "
            "recorded basis is indistinguishable from a guess, and this "
            "surface exists because 69 unexplained `unknown`s were."
        ),
    )


__all__ = ["ModelRuntimeErrorFingerprintRow"]
