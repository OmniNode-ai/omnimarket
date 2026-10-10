# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""File-backed cursor store of node_github_schedule_observer_effect (OMN-20803)."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import ValidationError

from omnimarket.nodes.node_github_schedule_observer_effect.models.model_schedule_observer_state import (
    ModelScheduleObserverState,
)


class FileScheduleObserverStateStore:
    """Persist the observer's state as one JSON file, replaced atomically."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> ModelScheduleObserverState:
        if not self._path.exists():
            return ModelScheduleObserverState()
        try:
            return ModelScheduleObserverState.model_validate_json(
                self._path.read_text(encoding="utf-8")
            )
        except (ValidationError, OSError) as exc:
            raise ValueError(
                f"observer state {self._path} cannot be read; not resetting the "
                f"cursors: {exc}"
            ) from None

    def save(self, state: ModelScheduleObserverState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f"{self._path.name}.tmp")
        tmp.write_text(state.model_dump_json(indent=1), encoding="utf-8")
        os.replace(tmp, self._path)
