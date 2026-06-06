# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Result model for deterministic context experiment assembly."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_context_experiment_compute.models.model_context_pack_extended import (
    ModelContextPackExtended,
)


class ModelContextExperimentResult(BaseModel):
    """Context experiment result returned by HandlerContextExperiment."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    status: Literal["ok", "failed"]
    packs: tuple[ModelContextPackExtended, ...] | None = None
    failure_class: str | None = None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


__all__ = ["ModelContextExperimentResult"]
