# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hold the last run and the counts of each scheduled job (OMN-19998)."""

from __future__ import annotations

from omnimarket.handlers.schedule_run_store import get_schedule_run_store
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunApplied,
    ModelScheduleRunEvent,
)
from omnimarket.protocols.protocol_schedule_run_store import (
    ProtocolScheduleRunStore,
)


class HandlerScheduleRunReducer:
    """Folds a schedule run event into the bound store when it is newer than the held one."""

    def __init__(self, store: ProtocolScheduleRunStore | None = None) -> None:
        self._store = store

    def handle(self, request: ModelScheduleRunEvent) -> ModelScheduleRunApplied:
        store = self._store or get_schedule_run_store()
        return store.apply(request)
