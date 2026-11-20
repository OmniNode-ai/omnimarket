# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two inputs of ``node_eval_board_compute``: the projection's snapshot and the display overlay."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.eval_item.model_eval_snapshot import (
    ModelEvalSnapshot,
)
from omnimarket.nodes.node_eval_board_compute.models.model_eval_board_overlay import (
    ModelEvalBoardOverlay,
)


class ModelEvalBoardRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot: ModelEvalSnapshot
    overlay: ModelEvalBoardOverlay


__all__ = ["ModelEvalBoardRequest"]
