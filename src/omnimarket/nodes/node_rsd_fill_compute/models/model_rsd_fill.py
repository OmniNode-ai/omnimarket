# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Re-export shim for contract.yaml model references.

Models have been split into individual files per ONEX architecture rules.
Import directly from the individual model files instead.
"""

from __future__ import annotations

from omnimarket.nodes.node_rsd_fill_compute.models.model_rsd_fill_input import (
    ModelRsdFillInput,
)
from omnimarket.nodes.node_rsd_fill_compute.models.model_rsd_fill_output import (
    ModelRsdFillOutput,
)
from omnimarket.nodes.node_rsd_fill_compute.models.model_scored_ticket import (
    ModelScoredTicket,
)

__all__: list[str] = [
    "ModelRsdFillInput",
    "ModelRsdFillOutput",
    "ModelScoredTicket",
]
