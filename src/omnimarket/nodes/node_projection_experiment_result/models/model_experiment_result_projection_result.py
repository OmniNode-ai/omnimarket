# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rows derived by the pure fold, to be persisted by the effect writer."""

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_projection_experiment_result.models.model_experiment_result_row import (
    ModelExperimentResultRow,
)


class ModelExperimentResultProjectionResult(BaseModel):
    """Rows derived by the pure fold, to be persisted by the effect writer."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    row: ModelExperimentResultRow
