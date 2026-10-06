# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Export of the shared ``omnimarket.models.lab_job`` reduce models for this node.

The definitions live in :mod:`omnimarket.models.lab_job` so the orchestrator,
checker and projection import them without reaching into this node's package.
"""

from __future__ import annotations

from omnimarket.models.lab_job import (
    ModelLabJobReduceInput,
    ModelLabJobReduceOutput,
)

__all__: list[str] = [
    "ModelLabJobReduceInput",
    "ModelLabJobReduceOutput",
]
