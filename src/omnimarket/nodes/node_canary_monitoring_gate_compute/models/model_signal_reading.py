# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelSignalReading — one observed monitoring signal sample (OMN-14735, B10).

A signal reading is a single numeric observation for one of the five
canary-monitoring signal domains the managed-staging execution plan names
(auth, TLS, broker, lag, RDS — see
``docs/plans/2026-07-17-managed-staging-verified-state-and-task-split.md`` A6/
B10). The model carries no live-bus/AWS dependency; it is a plain, frozen,
self-contained value so the gate can be exercised entirely in-process/in-test.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# The five signal domains A6 is expected to supply thresholds for. Adding a
# domain here is a contract change, not a threshold-value change.
SignalName = Literal["auth", "tls", "broker", "lag", "rds"]


class ModelSignalReading(BaseModel):
    """A single observed sample for one monitoring signal domain.

    Attributes:
        signal_name: Which of the five canary-monitoring signal domains this
            reading belongs to.
        value: The observed numeric value, in ``unit``.
        unit: The unit of ``value`` (e.g. ``"seconds"``, ``"count"``,
            ``"ratio"``). Free text — A6 defines the authoritative unit per
            signal; this field only records what was actually observed.
        source: Free-text description of where the reading came from (e.g. a
            CloudWatch metric name or probe identifier). Never a secret value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_name: SignalName = Field(
        ..., description="Which monitoring signal domain this reading belongs to."
    )
    value: float = Field(..., description="Observed numeric value, in `unit`.")
    unit: str = Field(default="", description="Unit of `value` (free text).")
    source: str = Field(
        default="", description="Where the reading came from (metric/probe id)."
    )


__all__ = ["ModelSignalReading", "SignalName"]
