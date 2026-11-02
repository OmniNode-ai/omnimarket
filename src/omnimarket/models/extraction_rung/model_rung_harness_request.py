# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the extraction rung harness (OMN-21140, plan task TE.3)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.extraction_rung.model_chunk_derivation_params import (
    ModelChunkDerivationParams,
)
from omnimarket.models.extraction_rung.model_recorded_rung_call import (
    ModelRecordedRungCall,
)
from omnimarket.models.extraction_rung.model_rung_window_measurement import (
    ModelRungWindowMeasurement,
)


class ModelRungHarnessRequest(BaseModel):
    """One rung's recorded calls, its contracted model id and its measured window.

    contracted_model_id is the served model the routing contract resolves for the rung.
    window is None when the rung has no measurement yet.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rung_id: str = Field(min_length=1)
    contracted_model_id: str = Field(min_length=1)
    calls: tuple[ModelRecordedRungCall, ...]
    window: ModelRungWindowMeasurement | None = None
    derivation: ModelChunkDerivationParams
