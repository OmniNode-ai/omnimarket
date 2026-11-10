# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLabTestHarnessReading: the harness reading at a read time.

Takes the stored reading (or none) and the time it is read at, and returns the reading as
it stands then: WORKING while fresh, NOT WORKING once past its max age, UNKNOWN when nothing
was ever published. The floor and the daily line read this; the staleness rule is
``reading_at``, shared with the harness effect's ``read`` verb so both give one answer.
"""

from __future__ import annotations

from omnimarket.models.lab_test_harness import (
    ModelLabTestHarnessFreshnessRequest,
    ModelLabTestHarnessReading,
    reading_at,
)


class HandlerLabTestHarnessReading:
    """COMPUTE handler: no clock read (the request carries the time), no state, no I/O."""

    def handle(
        self, request: ModelLabTestHarnessFreshnessRequest
    ) -> ModelLabTestHarnessReading:
        return reading_at(
            request.stored,
            piece=request.piece,
            tag=request.tag,
            max_age_seconds=request.max_age_seconds,
            now=request.now,
        )


__all__: list[str] = ["HandlerLabTestHarnessReading"]
