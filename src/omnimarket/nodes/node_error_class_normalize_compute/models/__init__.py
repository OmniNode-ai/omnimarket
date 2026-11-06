# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire types live in the shared model package for producer integration."""

from omnimarket.models.error_normalizer import (
    ModelErrorClassNormalizeRequest,
    ModelErrorClassNormalizeResult,
)

__all__ = ["ModelErrorClassNormalizeRequest", "ModelErrorClassNormalizeResult"]
