# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output of the pure fingerprint derivation (def-B response)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_runtime_error_fingerprints.models.model_runtime_error_fingerprint_row import (
    ModelRuntimeErrorFingerprintRow,
)


class ModelRuntimeErrorFingerprintResult(BaseModel):
    """The row the writer should upsert, and what the derivation concluded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelRuntimeErrorFingerprintRow = Field(
        ..., description="The fingerprint row to upsert."
    )
    producer_category_disagreed: bool = Field(
        ...,
        description=(
            "True when the producer's stamped category differs from the "
            "derived one. Not an error — it is the measurement that justifies "
            "deriving at all, and on the lab surface it is true for every row."
        ),
    )


__all__ = ["ModelRuntimeErrorFingerprintResult"]
