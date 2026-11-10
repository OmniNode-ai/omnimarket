# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders for the floor route tests (OMN-23072)."""

from __future__ import annotations

from datetime import datetime

from omnimarket.models.floor_17a import ModelFloorSimpleOverlay
from omnimarket.models.floor_17a.model_floor_document_event import (
    ModelFloorDocumentApplied,
    ModelFloorSimpleDocumentEvent,
)
from omnimarket.nodes.node_floor_document_reducer.handlers.handler_floor_document_reducer import (
    HandlerFloorDocumentReducer,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
)
from omnimarket.nodes.node_floor_simple_scheduled_effect.handlers.handler_floor_simple_scheduled import (
    UnreadFloorReadings,
)
from tests.nodes.node_floor_simple_effect.support import overlay_text


def floor_overlay() -> ModelFloorSimpleOverlay:
    return ModelFloorSimpleOverlay.model_validate(
        overlay_text("node_floor_simple_compute")
    )


def document_event(computed_at: datetime) -> ModelFloorSimpleDocumentEvent:
    document = HandlerFloorSimple().handle(
        UnreadFloorReadings().read(computed_at, floor_overlay())
    )
    return ModelFloorSimpleDocumentEvent(
        computed_at=computed_at, document=document, tick_id="tick-fixture"
    )


def project(event: ModelFloorSimpleDocumentEvent) -> ModelFloorDocumentApplied:
    return HandlerFloorDocumentReducer().handle(event)
