# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerTestSelectorReading: the selector reading per repository at a read time."""

from __future__ import annotations

from omnimarket.models.test_selector_reading import (
    ModelTestSelectorReading,
    ModelTestSelectorReadingRequest,
    read_selector,
)


class HandlerTestSelectorReading:
    """COMPUTE handler: no clock read (the request carries the time), no state, no I/O."""

    def handle(
        self, request: ModelTestSelectorReadingRequest
    ) -> ModelTestSelectorReading:
        return read_selector(request)


__all__: list[str] = ["HandlerTestSelectorReading"]
