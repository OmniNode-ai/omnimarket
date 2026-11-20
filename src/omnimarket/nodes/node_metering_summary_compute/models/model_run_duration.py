# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24615 AC8: the one run duration a local page shows, and what it measures.

The audit found the Runs table and the run detail's Duration showing latency_ms (the provider call) while the
detail's Execution tile showed execution_ms (the whole run). The backend names one: execution_ms, the time the
handler spent on the run from pickup to its terminal event, labelled Duration. A page reads the column, label and definition from the
metering summary and shows that column wherever it shows a run's duration; latency_ms stays the model call time.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

RUN_DURATION_COLUMN: Literal["execution_ms"] = "execution_ms"
RUN_DURATION_LABEL = "Duration"
RUN_DURATION_DEFINITION = (
    "Milliseconds the handler spent on the run, from pickup to its terminal event. "
    "Absent means not measured."
)


class ModelRunDuration(BaseModel):
    """The served run duration: the delegation_events column every page shows, its label and definition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    column: Literal["execution_ms"] = RUN_DURATION_COLUMN
    label: str = RUN_DURATION_LABEL
    definition: str = RUN_DURATION_DEFINITION
