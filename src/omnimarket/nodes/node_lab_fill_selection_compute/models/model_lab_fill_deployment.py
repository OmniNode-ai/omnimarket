# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Re-export: the model lives in omnimarket.models.lab_fill, shared across nodes."""

from omnimarket.models.lab_fill.model_lab_fill_deployment import (
    ModelLabFillDeployment,
)

__all__ = [
    "ModelLabFillDeployment",
]
