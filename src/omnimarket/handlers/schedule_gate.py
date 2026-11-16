# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The schedule gate: when a scheduled job is due, and the run event it emits."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Literal

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
    ModelScheduleRunEvent,
)


class ScheduleGate:
    """Holds the last run of one scheduled job and decides when the next is due."""

    def __init__(
        self,
        node_name: str,
        *,
        declaration: ModelScheduleDeclaration | None = None,
        environ: Mapping[str, str] | None = None,
        model: type[ModelScheduleDeclaration] = ModelScheduleDeclaration,
    ) -> None:
        self._node_name = node_name
        self._declaration = declaration
        self._environ = environ
        self._model = model
        self._last_run: datetime | None = None

    def declaration(self) -> ModelScheduleDeclaration:
        if self._declaration is None:
            self._declaration = load_structured_overlay(
                self._node_name,
                self._model,
                self._environ,
            )
        return self._declaration

    def due(self, now: datetime) -> bool:
        interval = timedelta(seconds=self.declaration().run_interval_seconds)
        if self._last_run is None:
            return True
        return now - self._last_run >= interval

    def mark(self, now: datetime) -> None:
        self._last_run = now

    def rearm(self) -> None:
        """Forget the last run, so the next tick is due: a run that never sent must not hold off the retry."""
        self._last_run = None

    def run_event(
        self,
        *,
        tick_id: str,
        scheduled_for: datetime,
        fired_at: datetime,
        outcome: Literal["completed", "failed"],
        detail: str = "",
    ) -> ModelScheduleRunEvent:
        decl = self.declaration()
        return ModelScheduleRunEvent(
            job_id=decl.job_id,
            node_name=self._node_name,
            tick_id=tick_id,
            scheduled_for=scheduled_for,
            fired_at=fired_at,
            outcome=outcome,
            detail=detail,
        )
