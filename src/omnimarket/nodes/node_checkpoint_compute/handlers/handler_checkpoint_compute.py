# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCheckpointCompute — session checkpoint projection access.

ONEX node type: COMPUTE — deterministic projection access, no LLM calls.
Ticket: OMN-12226
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_checkpoint_compute.models.model_checkpoint_request import (
    ModelCheckpointRequest,
)
from omnimarket.nodes.node_checkpoint_compute.models.model_checkpoint_result import (
    ModelCheckpointResult,
)
from omnimarket.nodes.session_state_projection import CheckpointProjectionStore


class HandlerCheckpointCompute:
    """Save, load, and list checkpoint projections in Onex state."""

    def __init__(
        self,
        store: CheckpointProjectionStore | None = None,
        state_dir: Path | str | None = None,
    ) -> None:
        self._store = store or CheckpointProjectionStore(state_dir=state_dir)

    def handle(self, request: ModelCheckpointRequest) -> ModelCheckpointResult:
        action = request.action.lower().strip()
        if action == "save":
            if request.payload is None:
                raise ValueError("payload is required for checkpoint save")
            self._store.save(request.checkpoint_id, request.payload)
            return ModelCheckpointResult(
                checkpoint_id=request.checkpoint_id,
                action=action,
                data=None,
                checkpoint_list=[],
            )

        if action == "load":
            return ModelCheckpointResult(
                checkpoint_id=request.checkpoint_id,
                action=action,
                data=self._store.load(request.checkpoint_id),
                checkpoint_list=[],
            )

        if action == "list":
            return ModelCheckpointResult(
                checkpoint_id=request.checkpoint_id,
                action=action,
                data=None,
                checkpoint_list=self._store.list_ids(),
            )

        raise ValueError("action must be one of: save, load, list")
