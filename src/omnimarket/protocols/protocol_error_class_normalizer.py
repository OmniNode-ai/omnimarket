# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""DI boundary used by error-producing nodes."""

from typing import Protocol

from omnimarket.models.error_normalizer import (
    ModelErrorClassNormalizeRequest,
    ModelErrorClassNormalizeResult,
)


class ProtocolErrorClassNormalizer(Protocol):
    def handle(
        self, request: ModelErrorClassNormalizeRequest
    ) -> ModelErrorClassNormalizeResult: ...
