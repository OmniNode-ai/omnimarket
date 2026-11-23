# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The sender's record of the nights it has settled, as one JSON file under the runtime's writable state directory."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.models.model_nightly_sender import (
    ModelSenderState,
    ModelSlotRecord,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.protocols.protocol_nightly_sender import (
    SenderStateError,
)

STATE_SUBDIRECTORY = "node_test_selector_nightly_sender_scheduled_effect"
STATE_FILE_NAME = "settled_slots.json"


class FileSenderStateStore:
    """Reads and writes the record in ``<state dir>/<node name>/settled_slots.json``; no fallback directory."""

    def __init__(self, *, state_dir_env: str, environ: Mapping[str, str]) -> None:
        value = environ.get(state_dir_env, "").strip()
        if not value:
            raise SenderStateError(
                f"{state_dir_env} names no writable state directory; the sender will not send without a record"
            )
        self._env = state_dir_env
        self._path = Path(value) / STATE_SUBDIRECTORY / STATE_FILE_NAME

    def _load(self) -> ModelSenderState:
        if not self._path.exists():
            return ModelSenderState()
        try:
            return ModelSenderState.model_validate_json(
                self._path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValidationError) as exc:
            raise SenderStateError(
                f"the sent record under {self._env} is unreadable ({type(exc).__name__})"
            ) from exc

    def _write(self, state: ModelSenderState) -> None:
        temporary = self._path.with_suffix(".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(state.model_dump_json(), encoding="utf-8")
            os.replace(temporary, self._path)
        except OSError as exc:
            raise SenderStateError(
                f"the sent record under {self._env} cannot be written ({type(exc).__name__})"
            ) from exc

    def ensure_writable(self) -> None:
        self._write(self._load())

    def settled(self, slot_date: date) -> ModelSlotRecord | None:
        for slot in self._load().slots:
            if slot.slot_date == slot_date:
                return slot
        return None

    def record(self, slot: ModelSlotRecord) -> None:
        state = self._load()
        kept = tuple(s for s in state.slots if s.slot_date != slot.slot_date)
        self._write(ModelSenderState(slots=(*kept, slot)))


__all__: list[str] = ["FileSenderStateStore"]
