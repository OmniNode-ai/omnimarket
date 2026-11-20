# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read one projection publication the runtime delivers from its snapshot topic (OMN-25036).

The reader pass replays each snapshot topic from a host's login, which the runtime does not hold. For the projection
the contract subscribes to, the runtime delivers each publication itself and this handler reads it the way the pass
does: the publication must name the projection the overlay names for that topic, and it keeps the time its producer
stood by it. A publication that names another projection reads ``unread`` with the reason, never as data.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from importlib import resources
from typing import Any

import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.floor_projection import (
    ModelFloorProjectionEntry,
    ModelFloorProjectionEnvelope,
    ModelFloorProjectionReadOverlay,
)

NODE = "node_floor_projection_read_effect"


def _contract() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return dict(yaml.safe_load(text))


class HandlerFloorProjectionSnapshotRead:
    """One delivered publication, read against the overlay's bound for its topic."""

    def __init__(
        self,
        overlay: ModelFloorProjectionReadOverlay | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._overlay = overlay
        self._environ: Mapping[str, str] = (
            environ if environ is not None else os.environ
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def _read_overlay(self) -> ModelFloorProjectionReadOverlay:
        if self._overlay is None:
            spec = _contract()["runtime"]["overlay"]
            self._overlay = load_structured_overlay(
                spec["overlay_id"],
                ModelFloorProjectionReadOverlay,
                self._environ,
                file_env=spec["file_env"],
            )
        return self._overlay

    def handle(
        self, request: ModelFloorProjectionEnvelope
    ) -> ModelFloorProjectionEntry:
        topic = _contract()["runtime"]["integration_snapshot_topic"]
        overlay = self._read_overlay()
        (projection_id, spec) = next(
            (pid, s) for pid, s in overlay.projections.items() if s.topic == topic
        )
        read_at = self._clock()
        if request.projection_id != projection_id:
            return ModelFloorProjectionEntry(
                projection_id=projection_id,
                status="unread",
                max_age_s=spec.max_age_s,
                read_at=read_at,
                reason=f"{topic} holds a publication of {request.projection_id}, not of {projection_id}",
            )
        return ModelFloorProjectionEntry(
            projection_id=projection_id,
            status="read",
            max_age_s=spec.max_age_s,
            read_at=read_at,
            as_of=request.as_of,
            producer=request.producer,
            payload=request.payload,
        )


__all__ = ["HandlerFloorProjectionSnapshotRead"]
