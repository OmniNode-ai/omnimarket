# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerNightlyAttribution: attribute a night's red test files to the pull requests that skipped them."""

from __future__ import annotations

from omnimarket.models.test_selector_reading import (
    ModelNightlyAttribution,
    ModelNightlyAttributionRequest,
    attribute_nightly_failures,
)


class HandlerNightlyAttribution:
    """COMPUTE handler: no clock read (the request carries every time), no state, no I/O."""

    def handle(
        self, request: ModelNightlyAttributionRequest
    ) -> ModelNightlyAttribution:
        return attribute_nightly_failures(request)


__all__: list[str] = ["HandlerNightlyAttribution"]
