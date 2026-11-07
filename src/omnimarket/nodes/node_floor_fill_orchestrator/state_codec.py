# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract-declared ``state_io`` codec of node_floor_fill_orchestrator: the row as JSON, nothing else.

The row is ``ModelFloorFillState`` (``seq`` and the entries by key). Its version in the store is its ``seq``, so
a decode refuses a payload whose ``seq`` is not the stored version: a row that disagrees with its own version is
unreadable, never read as empty.
"""

from __future__ import annotations

from omnimarket.nodes.node_floor_fill_reducer.handlers import (
    ModelFloorFillState,
)


class StateIoCodec:
    """encode and decode the fill's one state row."""

    def encode(self, state: ModelFloorFillState) -> bytes:
        return state.model_dump_json().encode("utf-8")

    def decode(self, raw: bytes | str) -> ModelFloorFillState:
        return ModelFloorFillState.model_validate_json(raw)


__all__: list[str] = ["StateIoCodec"]
